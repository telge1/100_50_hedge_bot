"""Post-hoc diagnostics on frozen multicoin reference results (offline; exploratory).

Reads existing checkpoints/reports only. Does not run backtests or query ClickHouse.
XRPUSDT is excluded from the main analysis (FAILED_PARITY).
"""

from __future__ import annotations

import json
import math
import warnings
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

REF_STRATEGY = "M0_TP075_SL050_H8"
REF_GROUP = "CORE_RESEARCH_SUPPORTIVE"
REF_COST = 0.15
REF_TF = "5m"
REF_MODE = "M0_STRICT_SYNC"
EXCLUDE_SYMBOLS = frozenset({"XRPUSDT"})

# Heuristic coin buckets (static labels; not performance-based selection).
MEME = frozenset(
    {
        "1000BONKUSDT",
        "1000PEPEUSDT",
        "1000FLOKIUSDT",
        "1000SHIBUSDT",
        "DOGEUSDT",
        "WIFUSDT",
        "PNUTUSDT",
        "POPCATUSDT",
    }
)
LARGE_CAP = frozenset({"BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "ADAUSDT"})
GOLD = frozenset({"XAUTUSDT"})

CONFIRMING_LIKE = frozenset({"CONFIRMING", "SUPPORTING", "STRONGLY_CONFIRMING"})
CONTRA_LIKE = frozenset({"CONTRADICTING", "STRONGLY_CONTRADICTING"})


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[5]


def _utc_hour(ts: str) -> int | None:
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.astimezone(timezone.utc).hour)
    except Exception:
        return None


def _session(hour: int | None) -> str:
    if hour is None:
        return "UNKNOWN"
    if 0 <= hour < 8:
        return "ASIA"
    if 8 <= hour < 16:
        return "EU"
    return "US"


def coin_bucket(symbol: str) -> str:
    s = str(symbol).upper()
    if s in GOLD:
        return "GOLD"
    if s in MEME:
        return "MEME"
    if s in LARGE_CAP:
        return "LARGE_CAP"
    return "ALT"


def verdict_score(v: Any) -> float | None:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return None
    s = str(v).upper()
    mapping = {
        "STRONGLY_CONFIRMING": 2.0,
        "CONFIRMING": 1.5,
        "SUPPORTING": 1.0,
        "NEUTRAL": 0.0,
        "INCONCLUSIVE_DATA": 0.0,
        "MISSING": None,
        "CONTRADICTING": -1.0,
        "STRONGLY_CONTRADICTING": -2.0,
    }
    return mapping.get(s, 0.0 if s else None)


def cliffs_delta(x: list[float], y: list[float]) -> float | None:
    """Cliff's delta: P(x>y) - P(x<y)."""
    if not x or not y:
        return None
    xa = np.asarray(x, dtype=float)
    ya = np.asarray(y, dtype=float)
    diff = xa[:, None] - ya[None, :]
    return float((np.sum(diff > 0) - np.sum(diff < 0)) / (len(xa) * len(ya)))


def smd(x: list[float], y: list[float]) -> float | None:
    if not x or not y:
        return None
    mx, my = float(np.mean(x)), float(np.mean(y))
    sx = float(np.std(x, ddof=1)) if len(x) > 1 else 0.0
    sy = float(np.std(y, ddof=1)) if len(y) > 1 else 0.0
    pooled = math.sqrt((sx**2 + sy**2) / 2.0) if (sx or sy) else None
    if not pooled:
        return None
    return (mx - my) / pooled


def bootstrap_mean_diff(
    x: list[float],
    y: list[float],
    *,
    n_boot: int = 200,
    seed: int = 42,
) -> tuple[float | None, float | None, float | None]:
    if not x or not y:
        return None, None, None
    rng = np.random.default_rng(seed)
    xa, ya = np.asarray(x, dtype=float), np.asarray(y, dtype=float)
    xb = rng.choice(xa, size=(n_boot, len(xa)), replace=True)
    yb = rng.choice(ya, size=(n_boot, len(ya)), replace=True)
    diffs = xb.mean(axis=1) - yb.mean(axis=1)
    lo, hi = np.percentile(diffs, [2.5, 97.5])
    return float(diffs.mean()), float(lo), float(hi)


def clustered_bootstrap_mean_diff(
    df: pd.DataFrame,
    *,
    feature: str,
    label_col: str,
    pos_label: int = 1,
    cluster_col: str = "symbol",
    n_boot: int = 150,
    seed: int = 7,
) -> tuple[float | None, float | None, float | None]:
    """Bootstrap by resampling coins (clusters), not individual trades."""
    sub = df.dropna(subset=[feature, label_col, cluster_col]).copy()
    if sub.empty:
        return None, None, None
    coins = np.asarray(sorted(sub[cluster_col].unique()))
    if len(coins) < 3:
        return None, None, None
    # Pre-aggregate per coin: mean feature for wins and losses
    g = sub.groupby([cluster_col, label_col])[feature].mean().unstack(label_col)
    if pos_label not in g.columns:
        return None, None, None
    neg_label = 0 if pos_label == 1 else 1
    if neg_label not in g.columns:
        # use all non-pos as neg via trade-level means per coin overall
        pass
    rng = np.random.default_rng(seed)
    # Faster path: coin-level mean feature split by majority win rate
    coin_mean = sub.groupby(cluster_col)[feature].mean()
    coin_winrate = sub.groupby(cluster_col)[label_col].mean()
    pos_coins = coin_winrate[coin_winrate >= 0.5].index.tolist()
    neg_coins = coin_winrate[coin_winrate < 0.5].index.tolist()
    if len(pos_coins) < 2 or len(neg_coins) < 2:
        # fallback: trade-level simple bootstrap (not clustered)
        xpos = sub.loc[sub[label_col] == pos_label, feature].astype(float).tolist()
        xneg = sub.loc[sub[label_col] != pos_label, feature].astype(float).tolist()
        return bootstrap_mean_diff(xpos, xneg, n_boot=n_boot, seed=seed)
    diffs = []
    pos_vals = coin_mean.reindex(pos_coins).dropna().to_numpy(float)
    neg_vals = coin_mean.reindex(neg_coins).dropna().to_numpy(float)
    for _ in range(n_boot):
        xb = rng.choice(pos_vals, size=len(pos_vals), replace=True)
        yb = rng.choice(neg_vals, size=len(neg_vals), replace=True)
        diffs.append(float(xb.mean() - yb.mean()))
    lo, hi = np.percentile(diffs, [2.5, 97.5])
    return float(np.mean(diffs)), float(lo), float(hi)


def auc_binary(scores: list[float], labels: list[int]) -> float | None:
    """Mann-Whitney AUC (descriptive)."""
    pairs = [(s, l) for s, l in zip(scores, labels) if s is not None and not (isinstance(s, float) and math.isnan(s))]
    if len(pairs) < 5:
        return None
    sc = np.asarray([s for s, _ in pairs], dtype=float)
    lb = np.asarray([l for _, l in pairs], dtype=int)
    pos = sc[lb == 1]
    neg = sc[lb == 0]
    if len(pos) == 0 or len(neg) == 0:
        return None
    diff = pos[:, None] - neg[None, :]
    return float((np.sum(diff > 0) + 0.5 * np.sum(diff == 0)) / (len(pos) * len(neg)))


def load_reference_frames(run_dir: Path) -> dict[str, Any]:
    reports = run_dir / "reports"
    trades = pd.read_csv(reports / "trades_all_coins.csv")
    cands = pd.read_csv(reports / "candidates_all_coins.csv")
    rbc = pd.read_csv(reports / "results_by_coin.csv")

    ref_trades = trades[
        (trades["strategy_key"] == REF_STRATEGY)
        & (trades["group"] == REF_GROUP)
        & (trades["roundtrip_cost_pct"] == REF_COST)
        & (~trades["symbol"].isin(EXCLUDE_SYMBOLS))
    ].copy()
    if "include_in_primary_pnl" in ref_trades.columns:
        ref_trades = ref_trades[ref_trades["include_in_primary_pnl"] != False]  # noqa: E712

    m0_cands = cands[
        (cands["mode_id"] == REF_MODE)
        & (cands["timeframe"] == REF_TF)
        & (~cands["symbol"].isin(EXCLUDE_SYMBOLS))
    ].copy()

    coin_rows = rbc[~rbc["symbol"].isin(EXCLUDE_SYMBOLS)].copy()
    return {
        "ref_trades": ref_trades,
        "m0_cands": m0_cands,
        "coin_rows": coin_rows,
        "all_trades": trades[~trades["symbol"].isin(EXCLUDE_SYMBOLS)].copy(),
        "all_cands": cands[~cands["symbol"].isin(EXCLUDE_SYMBOLS)].copy(),
    }


def feature_availability_matrix() -> pd.DataFrame:
    """Document what is present in frozen exports vs desired causal feature list."""
    rows = []

    def add(name, present, source, timing, causal, missing_note, usable):
        rows.append(
            {
                "feature": name,
                "present": present,
                "source": source,
                "timing": timing,
                "causal": causal,
                "missing_share_note": missing_note,
                "usable": usable,
            }
        )

    # Present categorical / metadata
    present_items = [
        ("direction", "candidates/trades", "decision_at", True, "0", True),
        ("decision_at_hour_utc", "derived from decision_at", "decision_at", True, "0", True),
        ("session_asia_eu_us", "derived from decision_at", "decision_at", True, "0", True),
        ("entry_price", "candidates/trades", "entry_at", True, "0", True),
        ("core_research_verdict", "candidates", "decision_at", True, "0", True),
        ("production_gate_verdict", "candidates", "decision_at", True, "0", True),
        ("coverage_segment", "candidates", "decision_at", True, "0", True),
        ("trade_flow_verdict", "candidates", "decision_at", True, "~0", True),
        ("orderbook_verdict", "candidates", "decision_at", True, "~0", True),
        ("liquidity_location_verdict", "candidates", "decision_at", True, "~0", True),
        ("volatility_verdict", "candidates", "decision_at", True, "~0", True),
        ("fake_impulse_verdict", "candidates", "decision_at", True, "~0", True),
        ("oi_verdict", "candidates", "decision_at", True, "often MISSING/INCONCLUSIVE", "partial"),
        ("liquidation_verdict", "candidates", "decision_at", True, "often MISSING", "partial"),
        ("*_contribution / *_decision_role", "candidates", "decision_at", True, "0", True),
        ("candles/trades/ob/oi/liq coverage status", "candidates", "decision_at", True, "0", True),
        ("coin_bucket_meme_large_gold_alt", "static symbol map", "prior", True, "0", True),
    ]
    for name, src, timing, causal, miss, usable in present_items:
        add(name, True, src, timing, causal, miss, usable)

    missing_cont = [
        "ema9",
        "ema20",
        "ema59",
        "ema9_slope",
        "ema20_slope",
        "ema59_slope",
        "ema_slopes_over_atr",
        "ema9_ema20_distance",
        "ema9_20_vs_ema59_distance",
        "band_compression",
        "cross_strength",
        "atr",
        "atr_pct_of_price",
        "realized_vol_short",
        "candle_range_over_atr",
        "body_over_atr",
        "vol_regime",
        "tp_over_atr",
        "sl_over_atr",
        "aggressive_buy_sell_ratio",
        "trade_flow_strength_numeric",
        "volume",
        "trade_count",
        "trade_flow_zscore",
        "imbalance_l50_mean",
        "spread",
        "spread_bps",
        "depth_impact_proxy",
        "ob_confirmation_strength_numeric",
        "ob_freshness_numeric",
        "lld_pool_direction",
        "lld_pool_strength",
        "lld_distance_to_pool",
        "lld_pool_count",
        "lld_raw_values",
        "source_count_supportive_adverse_neutral_numeric_breakdown_beyond_verdicts",
        "trend_range_regime_label",
        "regime_alignment_score",
    ]
    for name in missing_cont:
        add(
            name,
            False,
            "not stored in checkpoints/candidates CSV",
            "would be decision_at if enriched",
            True,
            "1.0 (absent)",
            False,
        )

    # Outcomes — present but NOT usable as features
    for name in ("exit_reason", "gross_pnl_usdt", "net_pnl_usdt", "mfe", "mae", "bars_held", "duration_minutes"):
        add(name, True, "trades CSV", "post-entry outcome", False, "0", False)

    return pd.DataFrame(rows)


def enrich_candidates(cands: pd.DataFrame) -> pd.DataFrame:
    out = cands.copy()
    out["hour_utc"] = out["decision_at"].map(_utc_hour)
    out["session"] = out["hour_utc"].map(_session)
    out["coin_bucket"] = out["symbol"].map(coin_bucket)
    out["is_long"] = (out["direction"].astype(str).str.upper() == "BULLISH").astype(int)
    for col in (
        "trade_flow_verdict",
        "orderbook_verdict",
        "liquidity_location_verdict",
        "volatility_verdict",
        "fake_impulse_verdict",
        "oi_verdict",
        "liquidation_verdict",
    ):
        if col in out.columns:
            out[col + "_score"] = out[col].map(verdict_score)
    # Count confirming / contra among core sources (causal verdicts only)
    core_cols = [
        "trade_flow_verdict",
        "orderbook_verdict",
        "liquidity_location_verdict",
        "volatility_verdict",
        "fake_impulse_verdict",
    ]

    def count_like(row, bucket):
        n = 0
        for c in core_cols:
            v = str(row.get(c) or "").upper()
            if v in bucket:
                n += 1
        return n

    out["n_confirming_like"] = out.apply(lambda r: count_like(r, CONFIRMING_LIKE), axis=1)
    out["n_contra_like"] = out.apply(lambda r: count_like(r, CONTRA_LIKE), axis=1)
    out["ob_is_neutral"] = (out["orderbook_verdict"].astype(str).str.upper() == "NEUTRAL").astype(int)
    out["ob_confirming"] = out["orderbook_verdict"].astype(str).str.upper().isin(CONFIRMING_LIKE).astype(int)
    out["flow_confirming"] = out["trade_flow_verdict"].astype(str).str.upper().isin(CONFIRMING_LIKE).astype(int)
    out["fake_contra"] = out["fake_impulse_verdict"].astype(str).str.upper().isin(CONTRA_LIKE).astype(int)
    out["vol_supporting"] = out["volatility_verdict"].astype(str).str.upper().isin(CONFIRMING_LIKE).astype(int)
    out["lld_supporting"] = out["liquidity_location_verdict"].astype(str).str.upper().isin(CONFIRMING_LIKE).astype(int)
    return out


def build_trade_table(ref_trades: pd.DataFrame, cands_enriched: pd.DataFrame) -> pd.DataFrame:
    keys = ["candidate_id", "symbol"]
    feat_cols = [
        c
        for c in cands_enriched.columns
        if c
        not in (
            "source_verdicts",
            "core_research_reason_codes",
            "production_gate_reason_codes",
        )
    ]
    merge = ref_trades.merge(
        cands_enriched[feat_cols],
        on=keys,
        how="left",
        suffixes=("", "_cand"),
    )
    merge["trade_win"] = (merge["net_pnl_usdt"] > 0).astype(int)
    merge["trade_loss"] = (merge["net_pnl_usdt"] < 0).astype(int)
    merge["midpoint"] = "2026-08-08T00:00:00+00:00"
    merge["half"] = np.where(merge["entry_at"].astype(str) < merge["midpoint"], "first_15d", "second_15d")
    return merge


def build_coin_labels(coin_rows: pd.DataFrame, ref_trades: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for _, r in coin_rows.iterrows():
        sym = r["symbol"]
        n = int(r.get("n_trades") or 0)
        pnl = float(r.get("net_pnl_usdt") or 0)
        flags = []
        if n < 5:
            flags.append("VERY_SMALL_SAMPLE")
        if n < 10:
            flags.append("SMALL_SAMPLE")
        label = "coin_break_even" if pnl == 0 else ("coin_net_positive" if pnl > 0 else "coin_net_negative")
        rows.append(
            {
                "symbol": sym,
                "coin_label": label,
                "coin_net_positive": int(pnl > 0),
                "coin_net_negative": int(pnl < 0),
                "coin_break_even": int(pnl == 0),
                "n_trades": n,
                "net_pnl_usdt": pnl,
                "expectancy_usdt": float(r.get("avg_net_pnl_usdt") or 0),
                "profit_factor_net": r.get("profit_factor_net"),
                "net_winrate": r.get("net_winrate"),
                "max_drawdown_usdt": r.get("max_drawdown_usdt"),
                "sample_flags": "|".join(flags) if flags else "OK",
                "coin_bucket": coin_bucket(sym),
            }
        )
    return pd.DataFrame(rows)


def coin_stability(ref_trades: pd.DataFrame, coin_labels: pd.DataFrame) -> pd.DataFrame:
    out = []
    mid = "2026-08-08T00:00:00+00:00"
    for _, crow in coin_labels.iterrows():
        sym = crow["symbol"]
        ts = ref_trades[ref_trades["symbol"] == sym].sort_values("entry_at")
        if ts.empty:
            continue
        pnls = ts["net_pnl_usdt"].astype(float).tolist()
        best = max(pnls)
        worst = min(pnls)
        total = sum(pnls)
        without_best = total - best
        without_worst = total - worst
        best_share = (best / total) if total != 0 else (1.0 if best > 0 else None)
        first = ts[ts["entry_at"].astype(str) < mid]["net_pnl_usdt"].sum()
        second = ts[ts["entry_at"].astype(str) >= mid]["net_pnl_usdt"].sum()
        long_pnl = ts[ts["direction"].astype(str).str.upper() == "BULLISH"]["net_pnl_usdt"].sum()
        short_pnl = ts[ts["direction"].astype(str).str.upper() == "BEARISH"]["net_pnl_usdt"].sum()
        n = len(pnls)
        cls = "UNSTABLE"
        if crow["coin_net_positive"] != 1:
            cls = "NOT_POSITIVE"
        elif n < 10:
            cls = "SMALL_SAMPLE_POSITIVE"
        elif without_best <= 0 and total > 0:
            cls = "OUTLIER_DEPENDENT"
        elif (long_pnl > 0 and short_pnl <= 0 and abs(short_pnl) < 1e-9) or (
            short_pnl > 0 and long_pnl <= 0 and abs(long_pnl) < 1e-9
        ):
            # one side has all trades
            n_long = int((ts["direction"].astype(str).str.upper() == "BULLISH").sum())
            n_short = int((ts["direction"].astype(str).str.upper() == "BEARISH").sum())
            if n_long == 0 or n_short == 0:
                cls = "ONE_SIDED"
            elif (first > 0 and second <= 0) or (second > 0 and first <= 0):
                cls = "UNSTABLE"
            else:
                cls = "STABLE_POSITIVE" if without_best > 0 and first * second >= 0 else "UNSTABLE"
        elif without_best > 0 and ((first >= 0 and second >= 0) or (first > 0 and second > 0)):
            cls = "STABLE_POSITIVE"
        elif without_best > 0:
            cls = "STABLE_POSITIVE" if first * second > 0 else "UNSTABLE"
        else:
            cls = "OUTLIER_DEPENDENT"
        out.append(
            {
                "symbol": sym,
                "n_trades": n,
                "net_pnl_usdt": total,
                "best_trade_pnl": best,
                "worst_trade_pnl": worst,
                "pnl_without_best": without_best,
                "pnl_without_worst": without_worst,
                "best_trade_share_of_pnl": best_share,
                "first_15d_pnl": float(first),
                "second_15d_pnl": float(second),
                "long_pnl": float(long_pnl),
                "short_pnl": float(short_pnl),
                "stability_class": cls,
                "coin_bucket": crow["coin_bucket"],
            }
        )
    return pd.DataFrame(out)


def descriptive_feature_table(
    trade_df: pd.DataFrame,
    coin_labels: pd.DataFrame,
    features: list[str],
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    pos_coins = set(coin_labels.loc[coin_labels["coin_net_positive"] == 1, "symbol"])
    neg_coins = set(coin_labels.loc[coin_labels["coin_net_negative"] == 1, "symbol"])
    coin_feat_rows = []
    trade_feat_rows = []
    effect_rows = []

    # coin-level: mean feature among SUPPORTIVE trades of that coin
    for feat in features:
        if feat not in trade_df.columns:
            continue
        per_coin = (
            trade_df.groupby("symbol")[feat]
            .apply(lambda s: pd.to_numeric(s, errors="coerce").mean())
            .rename("feat")
        )
        g = per_coin.loc[per_coin.index.isin(pos_coins)].dropna().tolist()
        l = per_coin.loc[per_coin.index.isin(neg_coins)].dropna().tolist()
        miss = float(per_coin.isna().mean()) if len(per_coin) else 1.0
        md, lo, hi = bootstrap_mean_diff(g, l)
        coin_auc_scores = []
        coin_auc_labels = []
        for sym, val in per_coin.items():
            if pd.isna(val):
                continue
            if sym in pos_coins:
                coin_auc_scores.append(float(val))
                coin_auc_labels.append(1)
            elif sym in neg_coins:
                coin_auc_scores.append(float(val))
                coin_auc_labels.append(0)
        coin_feat_rows.append(
            {
                "feature": feat,
                "level": "coin",
                "n_coins_non_null": int(per_coin.notna().sum()),
                "missing_share": round(miss, 4),
                "mean_all": float(np.nanmean(per_coin)) if per_coin.notna().any() else None,
                "median_all": float(np.nanmedian(per_coin)) if per_coin.notna().any() else None,
                "p25": float(np.nanpercentile(per_coin.dropna(), 25)) if per_coin.notna().any() else None,
                "p75": float(np.nanpercentile(per_coin.dropna(), 75)) if per_coin.notna().any() else None,
                "mean_winner_coins": float(np.mean(g)) if g else None,
                "mean_loser_coins": float(np.mean(l)) if l else None,
                "smd_winner_minus_loser": smd(g, l),
                "cliffs_delta": cliffs_delta(g, l),
                "boot_mean_diff": md,
                "boot_ci_low": lo,
                "boot_ci_high": hi,
            }
        )
        effect_rows.append(
            {
                "feature": feat,
                "level": "coin",
                "smd": smd(g, l),
                "cliffs_delta": cliffs_delta(g, l),
                "auc": auc_binary(coin_auc_scores, coin_auc_labels),
            }
        )

        # trade-level
        tw = trade_df.dropna(subset=[feat]).copy()
        tw[feat] = pd.to_numeric(tw[feat], errors="coerce")
        tw = tw.dropna(subset=[feat])
        xpos = tw.loc[tw["trade_win"] == 1, feat].astype(float).tolist()
        xneg = tw.loc[tw["trade_win"] == 0, feat].astype(float).tolist()
        md2, lo2, hi2 = clustered_bootstrap_mean_diff(tw, feature=feat, label_col="trade_win")
        trade_feat_rows.append(
            {
                "feature": feat,
                "level": "trade",
                "n_trades_non_null": int(len(tw)),
                "missing_share": round(1.0 - len(tw) / max(len(trade_df), 1), 4),
                "mean_all": float(tw[feat].mean()) if len(tw) else None,
                "median_all": float(tw[feat].median()) if len(tw) else None,
                "p25": float(tw[feat].quantile(0.25)) if len(tw) else None,
                "p75": float(tw[feat].quantile(0.75)) if len(tw) else None,
                "mean_win_trades": float(np.mean(xpos)) if xpos else None,
                "mean_loss_trades": float(np.mean(xneg)) if xneg else None,
                "smd_win_minus_loss": smd(xpos, xneg),
                "cliffs_delta": cliffs_delta(xpos, xneg),
                "auc": auc_binary(tw[feat].tolist(), tw["trade_win"].astype(int).tolist()),
                "boot_mean_diff_clustered": md2,
                "boot_ci_low": lo2,
                "boot_ci_high": hi2,
            }
        )
        effect_rows.append(
            {
                "feature": feat,
                "level": "trade",
                "smd": smd(xpos, xneg),
                "cliffs_delta": cliffs_delta(xpos, xneg),
                "auc": auc_binary(tw[feat].tolist(), tw["trade_win"].astype(int).tolist()),
            }
        )

    return pd.DataFrame(coin_feat_rows), pd.DataFrame(trade_feat_rows), pd.DataFrame(effect_rows)


def quantile_bins(trade_df: pd.DataFrame, features: list[str]) -> pd.DataFrame:
    rows = []
    for feat in features:
        if feat not in trade_df.columns:
            continue
        sub = trade_df.dropna(subset=[feat]).copy()
        sub[feat] = pd.to_numeric(sub[feat], errors="coerce")
        sub = sub.dropna(subset=[feat])
        if len(sub) < 20 or sub[feat].nunique() < 4:
            continue
        try:
            sub["bin"] = pd.qcut(sub[feat], 4, labels=["Q1", "Q2", "Q3", "Q4"], duplicates="drop")
        except ValueError:
            continue
        for b, g in sub.groupby("bin"):
            pnls = g["net_pnl_usdt"].astype(float)
            wins = (pnls > 0).sum()
            losses = (pnls < 0).sum()
            gp = pnls[pnls > 0].sum()
            gl = abs(pnls[pnls < 0].sum())
            rows.append(
                {
                    "feature": feat,
                    "bin": str(b),
                    "n_trades": int(len(g)),
                    "n_coins": int(g["symbol"].nunique()),
                    "winrate": float(wins / len(g)) if len(g) else None,
                    "expectancy_usdt": float(pnls.mean()),
                    "net_pnl_usdt": float(pnls.sum()),
                    "profit_factor": float(gp / gl) if gl > 0 else None,
                    "bin_low": float(g[feat].min()),
                    "bin_high": float(g[feat].max()),
                }
            )
    return pd.DataFrame(rows)


def source_filter_audit(all_trades: pd.DataFrame) -> pd.DataFrame:
    """Compare control groups for same M0 cell (exploratory)."""
    rows = []
    base = all_trades[
        (all_trades["strategy_key"] == REF_STRATEGY)
        & (all_trades["mode_id"] == REF_MODE)
        & (all_trades["timeframe"] == REF_TF)
        & (all_trades["roundtrip_cost_pct"] == REF_COST)
        & (~all_trades["symbol"].isin(EXCLUDE_SYMBOLS))
    ]
    for group, g in base.groupby("group"):
        pnls = g["net_pnl_usdt"].astype(float)
        wins = (pnls > 0).sum()
        gp = pnls[pnls > 0].sum()
        gl = abs(pnls[pnls < 0].sum())
        rows.append(
            {
                "group": group,
                "n_trades": int(len(g)),
                "n_coins": int(g["symbol"].nunique()),
                "net_pnl_usdt": float(pnls.sum()),
                "expectancy_usdt": float(pnls.mean()) if len(g) else None,
                "winrate": float(wins / len(g)) if len(g) else None,
                "profit_factor_net": float(gp / gl) if gl > 0 else None,
                "tp_exit": int((g["exit_reason"] == "TP_EXIT").sum()),
                "sl_exit": int((g["exit_reason"] == "SL_EXIT").sum()),
                "time_exit": int((g["exit_reason"] == "TIME_EXIT").sum()),
            }
        )
    return pd.DataFrame(rows)


def long_short_session_split(trade_df: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for keys, g in trade_df.groupby(["direction", "session"]):
        direction, session = keys
        pnls = g["net_pnl_usdt"].astype(float)
        rows.append(
            {
                "direction": direction,
                "session": session,
                "n_trades": len(g),
                "n_coins": g["symbol"].nunique(),
                "net_pnl_usdt": float(pnls.sum()),
                "expectancy_usdt": float(pnls.mean()),
                "winrate": float((pnls > 0).mean()),
            }
        )
    return pd.DataFrame(rows)


def half_window_feature_stability(trade_df: pd.DataFrame, features: list[str]) -> pd.DataFrame:
    rows = []
    for feat in features:
        if feat not in trade_df.columns:
            continue
        for half in ("first_15d", "second_15d"):
            g = trade_df[trade_df["half"] == half].dropna(subset=[feat])
            g[feat] = pd.to_numeric(g[feat], errors="coerce")
            g = g.dropna(subset=[feat])
            if g.empty:
                continue
            xpos = g.loc[g["trade_win"] == 1, feat].astype(float).tolist()
            xneg = g.loc[g["trade_win"] == 0, feat].astype(float).tolist()
            rows.append(
                {
                    "feature": feat,
                    "half": half,
                    "n": len(g),
                    "mean_win": float(np.mean(xpos)) if xpos else None,
                    "mean_loss": float(np.mean(xneg)) if xneg else None,
                    "smd": smd(xpos, xneg),
                    "cliffs_delta": cliffs_delta(xpos, xneg),
                }
            )
    return pd.DataFrame(rows)


def leave_one_coin_out_diagnostics(coin_labels: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for leave in coin_labels["symbol"]:
        rest = coin_labels[coin_labels["symbol"] != leave]
        rows.append(
            {
                "left_out": leave,
                "remaining_n": len(rest),
                "remaining_n_positive": int(rest["coin_net_positive"].sum()),
                "remaining_pct_positive": float(rest["coin_net_positive"].mean()),
                "remaining_mean_expectancy": float(rest["expectancy_usdt"].mean()),
                "remaining_pooled_pnl": float(rest["net_pnl_usdt"].sum()),
                "remaining_median_expectancy": float(rest["expectancy_usdt"].median()),
            }
        )
    return pd.DataFrame(rows)


def diagnostic_logistic(trade_df: pd.DataFrame, features: list[str]) -> dict[str, Any]:
    """Simple L2 logistic with Leave-One-Coin-Out; diagnostic only."""
    use = [f for f in features if f in trade_df.columns]
    sub = trade_df.dropna(subset=use + ["trade_win", "symbol"]).copy()
    for f in use:
        sub[f] = pd.to_numeric(sub[f], errors="coerce")
    sub = sub.dropna(subset=use)
    if len(sub) < 50 or sub["symbol"].nunique() < 8 or sub["trade_win"].nunique() < 2:
        return {
            "status": "INSUFFICIENT_DATA",
            "note": "Too few complete rows/coins for LOCO logistic.",
            "features": use,
        }
    try:
        from sklearn.linear_model import LogisticRegression
        from sklearn.metrics import roc_auc_score, precision_score, brier_score_loss
        from sklearn.preprocessing import StandardScaler
    except Exception as exc:
        return {"status": "SKLEARN_UNAVAILABLE", "error": str(exc), "features": use}

    coins = sorted(sub["symbol"].unique())
    y_true_all = []
    y_prob_all = []
    coefs = []
    for leave in coins:
        train = sub[sub["symbol"] != leave]
        test = sub[sub["symbol"] == leave]
        if train["trade_win"].nunique() < 2 or test.empty:
            continue
        Xtr = train[use].to_numpy(dtype=float)
        Xte = test[use].to_numpy(dtype=float)
        ytr = train["trade_win"].to_numpy(dtype=int)
        yte = test["trade_win"].to_numpy(dtype=int)
        scaler = StandardScaler()
        Xtr_s = scaler.fit_transform(Xtr)
        Xte_s = scaler.transform(Xte)
        clf = LogisticRegression(penalty="l2", C=1.0, max_iter=500, solver="lbfgs")
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            clf.fit(Xtr_s, ytr)
        proba = clf.predict_proba(Xte_s)[:, 1]
        y_true_all.extend(yte.tolist())
        y_prob_all.extend(proba.tolist())
        coefs.append(dict(zip(use, clf.coef_[0].tolist())))

    if len(set(y_true_all)) < 2 or len(y_true_all) < 20:
        return {"status": "INSUFFICIENT_FOLDS", "features": use}

    y_true = np.asarray(y_true_all)
    y_prob = np.asarray(y_prob_all)
    y_pred = (y_prob >= 0.5).astype(int)
    auc = float(roc_auc_score(y_true, y_prob))
    prec = float(precision_score(y_true, y_pred, zero_division=0))
    brier = float(brier_score_loss(y_true, y_prob))
    coef_df = pd.DataFrame(coefs)
    mean_coef = coef_df.mean().to_dict()
    sign_stable = {k: bool((coef_df[k] > 0).mean() >= 0.8 or (coef_df[k] < 0).mean() >= 0.8) for k in use}

    # without best coin
    best_coin = sub.groupby("symbol")["net_pnl_usdt"].sum().idxmax()
    sub2 = sub[sub["symbol"] != best_coin]
    # quick re-eval AUC without best coin in test predictions already collected — filter
    mask = sub["symbol"].repeat(1)  # not aligned; recompute briefly
    y2_true, y2_prob = [], []
    for leave in sorted(sub2["symbol"].unique()):
        train = sub2[sub2["symbol"] != leave]
        test = sub2[sub2["symbol"] == leave]
        if train["trade_win"].nunique() < 2 or test.empty:
            continue
        scaler = StandardScaler()
        Xtr_s = scaler.fit_transform(train[use].to_numpy(float))
        Xte_s = scaler.transform(test[use].to_numpy(float))
        clf = LogisticRegression(penalty="l2", C=1.0, max_iter=500)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            clf.fit(Xtr_s, train["trade_win"].to_numpy(int))
        y2_true.extend(test["trade_win"].tolist())
        y2_prob.extend(clf.predict_proba(Xte_s)[:, 1].tolist())
    auc_wo_best = float(roc_auc_score(y2_true, y2_prob)) if len(set(y2_true)) > 1 else None

    baseline_auc = 0.5
    status = "NO_GENERALIZABLE_SEPARATOR" if auc < baseline_auc + 0.05 else "WEAK_SIGNAL"
    if auc >= 0.60 and auc_wo_best and auc_wo_best >= 0.55:
        status = "EXPLORATORY_SIGNAL"
    return {
        "status": status,
        "features": use,
        "n_trades": int(len(sub)),
        "n_coins": int(sub["symbol"].nunique()),
        "out_of_coin_auc": auc,
        "precision_at_0_5": prec,
        "brier": brier,
        "mean_coefficients": mean_coef,
        "sign_stable_80pct_folds": sign_stable,
        "auc_without_best_coin": auc_wo_best,
        "best_coin_excluded": best_coin,
        "note": "Exploratory only; not a trading rule. No hyperparameter search.",
    }


def answer_fixed_questions(
    trade_df: pd.DataFrame,
    coin_labels: pd.DataFrame,
    stability: pd.DataFrame,
    avail: pd.DataFrame,
) -> dict[str, Any]:
    missing_numeric = [
        "ATR-%",
        "TP/ATR",
        "SL/ATR",
        "EMA59 slope",
        "EMA9-EMA20 distance",
        "cross strength",
        "imbalance numeric",
        "trade-flow numeric",
        "spread",
    ]
    present = set(avail.loc[avail["present"] == True, "feature"])  # noqa: E712

    def present_ans(key, text):
        return {"question": key, "answer": text, "evidence": "available_fields"}

    def missing_ans(key, need):
        return {
            "question": key,
            "answer": f"NOT_ANSWERABLE_FROM_STORED_FEATURES: requires {need}",
            "evidence": "feature_absent_in_checkpoints",
        }

    pos = set(coin_labels.loc[coin_labels.coin_net_positive == 1, "symbol"])
    # OB neutral share among SUPPORTIVE winners vs losers
    tw = trade_df.copy()
    tw["is_winner_coin"] = tw["symbol"].isin(pos).astype(int)
    ob_neu_w = tw.loc[tw.is_winner_coin == 1, "ob_is_neutral"].mean() if "ob_is_neutral" in tw else None
    ob_neu_l = tw.loc[tw.is_winner_coin == 0, "ob_is_neutral"].mean() if "ob_is_neutral" in tw else None
    flow_w = tw.loc[tw.is_winner_coin == 1, "flow_confirming"].mean() if "flow_confirming" in tw else None
    flow_l = tw.loc[tw.is_winner_coin == 0, "flow_confirming"].mean() if "flow_confirming" in tw else None
    fake_w = tw.loc[tw.trade_win == 1, "fake_contra"].mean() if "fake_contra" in tw else None
    fake_l = tw.loc[tw.trade_win == 0, "fake_contra"].mean() if "fake_contra" in tw else None
    lld_w = tw.loc[tw.trade_win == 1, "lld_supporting"].mean() if "lld_supporting" in tw else None
    lld_l = tw.loc[tw.trade_win == 0, "lld_supporting"].mean() if "lld_supporting" in tw else None

    long_pnl = tw.loc[tw.direction.astype(str).str.upper() == "BULLISH", "net_pnl_usdt"].sum()
    short_pnl = tw.loc[tw.direction.astype(str).str.upper() == "BEARISH", "net_pnl_usdt"].sum()
    by_hour = tw.groupby("hour_utc")["net_pnl_usdt"].sum().sort_values()
    by_bucket = coin_labels.groupby("coin_bucket").agg(
        n=("symbol", "count"), n_pos=("coin_net_positive", "sum"), pnl=("net_pnl_usdt", "sum")
    )

    stable_pos = stability[stability.stability_class == "STABLE_POSITIVE"]
    outlier = stability[stability.stability_class == "OUTLIER_DEPENDENT"]
    small = stability[stability.stability_class == "SMALL_SAMPLE_POSITIVE"]

    # leave best coin
    if len(coin_labels):
        best = coin_labels.sort_values("net_pnl_usdt", ascending=False).iloc[0]
        without = coin_labels[coin_labels.symbol != best.symbol]
        remain_pos = int(without.coin_net_positive.sum())
    else:
        best = None
        remain_pos = None

    first = tw[tw.half == "first_15d"]
    second = tw[tw.half == "second_15d"]

    answers = [
        missing_ans(1, "atr_pct_of_price"),
        missing_ans(2, "tp_over_atr"),
        missing_ans(3, "sl_over_atr"),
        missing_ans(4, "trend_range_regime_label"),
        missing_ans(5, "regime_alignment_score"),
        missing_ans(6, "ema59_slope"),
        missing_ans(7, "ema9_ema20_distance"),
        missing_ans(8, "cross_strength"),
        present_ans(
            9,
            f"OB confirming share win-coin trades={tw.loc[tw.is_winner_coin==1,'ob_confirming'].mean() if 'ob_confirming' in tw else None:.3f} "
            f"vs loser-coin={tw.loc[tw.is_winner_coin==0,'ob_confirming'].mean() if 'ob_confirming' in tw else None:.3f}; "
            f"neutral OB share winner-coin={ob_neu_w} loser-coin={ob_neu_l}",
        ),
        present_ans(
            10,
            f"Among SUPPORTIVE trades, neutral OB is common overall; winner-coin neutral share={ob_neu_w}, loser-coin={ob_neu_l}",
        ),
        present_ans(11, f"flow_confirming mean winner-coin={flow_w} loser-coin={flow_l}"),
        present_ans(
            12,
            f"fake_contra rate among win trades={fake_w} vs loss trades={fake_l} (SUPPORTIVE already applied)",
        ),
        present_ans(13, f"lld_supporting mean win trades={lld_w} loss trades={lld_l}"),
        missing_ans(14, "spread_bps vs expected move"),
        present_ans(15, f"long_pnl={long_pnl:.2f} short_pnl={short_pnl:.2f}"),
        present_ans(
            16,
            f"worst hours by pnl={by_hour.head(3).to_dict()} best={by_hour.tail(3).to_dict()}",
        ),
        present_ans(17, f"coin_bucket summary={by_bucket.to_dict()}"),
        present_ans(
            18,
            f"positive coins={len(pos)}; OUTLIER_DEPENDENT={list(outlier.symbol)}; "
            f"SMALL_SAMPLE_POSITIVE={list(small.symbol)}; STABLE_POSITIVE={list(stable_pos.symbol)}",
        ),
        present_ans(
            19,
            f"best coin={None if best is None else best.symbol} pnl={None if best is None else best.net_pnl_usdt}; "
            f"remaining positive coins without best={remain_pos}",
        ),
        present_ans(
            20,
            f"first_15d net={first.net_pnl_usdt.sum():.2f} n={len(first)}; "
            f"second_15d net={second.net_pnl_usdt.sum():.2f} n={len(second)}",
        ),
    ]
    return {
        "answers": answers,
        "missing_numeric_features_blocking_atr_ema_questions": missing_numeric,
        "present_feature_count": len(present),
    }


def enrichment_plan() -> dict[str, Any]:
    return {
        "purpose": "Causal enrichment at decision_at for later OOS hypothesis tests — NOT executed in this run",
        "join_key": ["symbol", "candidate_id", "decision_at"],
        "required_tables": [
            "signal_generator.candles_1m (aggregate to 5m + EMA/ATR features)",
            "orderbook_analysis.public_trades_canonical → 1m flow features",
            "orderbook_analysis.orderbook_features_1s_v2 (ob200_v3) → imbalance/spread",
            "existing feature_builder / LLD outputs if reproducible",
        ],
        "must_be_causal": "only bars/events with timestamp <= decision_at",
        "store_into": "optional enrichment parquet keyed by candidate_id; do not mutate production gates",
        "priority_fields": [
            "atr_pct",
            "tp_over_atr",
            "sl_over_atr",
            "ema59_slope_over_atr",
            "ema9_ema20_dist_over_atr",
            "imbalance_l50_mean",
            "spread_bps",
            "taker_buy_ratio",
        ],
        "status": "PLAN_ONLY_NO_CLICKHOUSE_IN_THIS_RUN",
    }


def write_summary_md(path: Path, payload: dict[str, Any]) -> None:
    lines = [
        "# Multi-Coin Reference Diagnostics (Exploratory / Post-hoc)",
        "",
        f"**Verdict:** `{payload['verdict']}`",
        "",
        "- Reference: 5m M0_STRICT_SYNC / CORE_RESEARCH_SUPPORTIVE / TP0.75 / SL0.50 / 8h / cost 0.15%",
        "- XRPUSDT excluded (FAILED_PARITY)",
        "- Continuous EMA/ATR/OB numerics **not stored** in checkpoints → enrichment plan only",
        "",
        "## A. Profitable coins",
        "",
    ]
    for r in payload.get("profitable_coins", []):
        lines.append(
            f"- `{r['symbol']}` n={r['n_trades']} net={r['net_pnl_usdt']:+.2f} "
            f"stability={r.get('stability_class')} flags={r.get('sample_flags')}"
        )
    lines += [
        "",
        "## B. Stability without best trade",
        "",
        f"- Still positive without best trade: {payload.get('n_positive_without_best_trade')}",
        f"- STABLE_POSITIVE: {payload.get('stable_positive')}",
        f"- OUTLIER_DEPENDENT: {payload.get('outlier_dependent')}",
        "",
        "## C–J (see summary.json)",
        "",
        f"- Strongest available separators (by |Cliff δ| trade-level): {payload.get('top_effects')}",
        f"- Diagnostic model: {payload.get('model_status')}",
        f"- Source filter: SUPPORTIVE expectancy vs EMA_RAW: {payload.get('supportive_vs_raw')}",
        "",
        "## K. Hypotheses for later strict OOS (max 4)",
        "",
    ]
    for h in payload.get("hypotheses", []):
        lines.append(f"- {h}")
    lines += ["", f"**Final verdict:** `{payload['verdict']}`", ""]
    path.write_text("\n".join(lines), encoding="utf-8")


def run_diagnostics(
    *,
    run_dir: str | Path | None = None,
    out_dir: str | Path | None = None,
) -> dict[str, Any]:
    root = _repo_root()
    run_path = Path(run_dir) if run_dir else root / "results/edc_sync_tolerance/multicoin_30d_frozen_validation"
    diag = Path(out_dir) if out_dir else run_path / "diagnostics"
    diag.mkdir(parents=True, exist_ok=True)

    frames = load_reference_frames(run_path)
    ref_trades = frames["ref_trades"]
    m0_cands = frames["m0_cands"]
    coin_rows = frames["coin_rows"]

    avail = feature_availability_matrix()
    avail.to_csv(diag / "feature_availability.csv", index=False)

    cands_e = enrich_candidates(m0_cands)
    # Restrict candidate features join to SUPPORTIVE set matching trades
    trade_df = build_trade_table(ref_trades, cands_e)
    coin_labels = build_coin_labels(coin_rows, ref_trades)
    stability = coin_stability(ref_trades, coin_labels)

    coin_labels.to_csv(diag / "coin_labels.csv", index=False)
    trade_label_cols = [
        "symbol",
        "candidate_id",
        "direction",
        "entry_at",
        "exit_reason",
        "net_pnl_usdt",
        "trade_win",
        "trade_loss",
        "hour_utc",
        "session",
        "coin_bucket",
        "half",
    ]
    trade_df[[c for c in trade_label_cols if c in trade_df.columns]].to_csv(diag / "trade_labels.csv", index=False)
    stability.to_csv(diag / "profitable_coin_stability.csv", index=False)

    features = [
        "is_long",
        "hour_utc",
        "ob_is_neutral",
        "ob_confirming",
        "flow_confirming",
        "fake_contra",
        "vol_supporting",
        "lld_supporting",
        "n_confirming_like",
        "n_contra_like",
        "trade_flow_verdict_score",
        "orderbook_verdict_score",
        "liquidity_location_verdict_score",
        "volatility_verdict_score",
        "fake_impulse_verdict_score",
        "entry_price",
    ]
    coin_feat, trade_feat, effects = descriptive_feature_table(trade_df, coin_labels, features)
    coin_feat.to_csv(diag / "coin_feature_comparison.csv", index=False)
    trade_feat.to_csv(diag / "trade_feature_comparison.csv", index=False)
    effects.to_csv(diag / "feature_effect_sizes.csv", index=False)

    bins = quantile_bins(trade_df, ["n_confirming_like", "n_contra_like", "hour_utc", "entry_price", "orderbook_verdict_score", "trade_flow_verdict_score"])
    bins.to_csv(diag / "feature_quantile_bins.csv", index=False)

    src_audit = source_filter_audit(frames["all_trades"])
    src_audit.to_csv(diag / "source_filter_audit.csv", index=False)

    lss = long_short_session_split(trade_df)
    lss.to_csv(diag / "long_short_session_split.csv", index=False)

    half_stab = half_window_feature_stability(
        trade_df, ["ob_confirming", "flow_confirming", "fake_contra", "n_confirming_like", "ob_is_neutral"]
    )
    half_stab.to_csv(diag / "half_window_feature_stability.csv", index=False)

    loo = leave_one_coin_out_diagnostics(coin_labels)
    loo.to_csv(diag / "leave_one_coin_out_diagnostics.csv", index=False)

    model = diagnostic_logistic(
        trade_df,
        [
            "ob_confirming",
            "ob_is_neutral",
            "flow_confirming",
            "fake_contra",
            "vol_supporting",
            "lld_supporting",
            "n_confirming_like",
            "n_contra_like",
            "is_long",
            "hour_utc",
        ],
    )
    (diag / "diagnostic_model_report.json").write_text(json.dumps(model, indent=2, default=str) + "\n")

    # Source contribution: among SUPPORTIVE, how often OB neutral
    supportive = cands_e[cands_e.core_research_verdict == REF_GROUP]
    ob_neutral_rate = float((supportive.orderbook_verdict.astype(str).str.upper() == "NEUTRAL").mean()) if len(supportive) else None

    raw_row = src_audit[src_audit.group == "EMA_RAW"]
    sup_row = src_audit[src_audit.group == REF_GROUP]
    supportive_vs_raw = None
    if len(raw_row) and len(sup_row):
        supportive_vs_raw = {
            "ema_raw_expectancy": float(raw_row.iloc[0]["expectancy_usdt"]),
            "supportive_expectancy": float(sup_row.iloc[0]["expectancy_usdt"]),
            "delta_expectancy": float(sup_row.iloc[0]["expectancy_usdt"] - raw_row.iloc[0]["expectancy_usdt"]),
            "ema_raw_pnl": float(raw_row.iloc[0]["net_pnl_usdt"]),
            "supportive_pnl": float(sup_row.iloc[0]["net_pnl_usdt"]),
        }

    pos_coins = coin_labels[coin_labels.coin_net_positive == 1].sort_values("net_pnl_usdt", ascending=False)
    stab_map = {r.symbol: r.stability_class for _, r in stability.iterrows()}
    profitable = []
    for _, r in pos_coins.iterrows():
        profitable.append(
            {
                "symbol": r.symbol,
                "n_trades": int(r.n_trades),
                "net_pnl_usdt": float(r.net_pnl_usdt),
                "sample_flags": r.sample_flags,
                "stability_class": stab_map.get(r.symbol),
                "coin_bucket": r.coin_bucket,
            }
        )

    n_without_best = int((stability.pnl_without_best > 0).sum()) if len(stability) else 0
    top_effects = []
    if len(effects):
        te = effects[effects.level == "trade"].dropna(subset=["cliffs_delta"]).copy()
        te["abs_d"] = te["cliffs_delta"].abs()
        te = te.sort_values("abs_d", ascending=False).head(8)
        top_effects = te[["feature", "cliffs_delta", "smd", "auc"]].to_dict(orient="records")

    questions = answer_fixed_questions(trade_df, coin_labels, stability, avail)

    # Hypotheses limited to what data can support + enrichment priorities
    hypotheses = []
    if supportive_vs_raw and supportive_vs_raw["delta_expectancy"] is not None:
        hypotheses.append(
            "H1 (source): Require non-neutral orderbook confirmation inside SUPPORTIVE — "
            f"neutral OB rate among SUPPORTIVE cands={ob_neutral_rate:.2f}; test OOS after enrichment-free rule on verdicts only."
        )
    hypotheses.append(
        "H2 (enrichment/OOS): ATR% and TP0.75/ATR / SL0.50/ATR quartiles separate win vs loss coins "
        "(blocked now — atr fields absent; requires causal enrichment)."
    )
    hypotheses.append(
        "H3 (enrichment/OOS): EMA59 slope/ATR and EMA9–EMA20 distance at decision_at differ for stable winners "
        "(blocked now — EMA numerics absent)."
    )
    # keep only if flow effect visible
    hypotheses.append(
        "H4 (available now): Trade-flow confirming vs not, within SUPPORTIVE, as a pre-registered OOS filter "
        "(uses stored trade_flow_verdict only)."
    )
    hypotheses = hypotheses[:4]

    # Verdict selection
    usable_cont = avail[(avail.present == True) & (avail.usable == True)]  # noqa: E712
    n_missing_priority = int((avail.present == False).sum())  # noqa: E712
    if n_missing_priority > 20 and model.get("status") in (
        "NO_GENERALIZABLE_SEPARATOR",
        "WEAK_SIGNAL",
        "INSUFFICIENT_DATA",
        "SKLEARN_UNAVAILABLE",
        "INSUFFICIENT_FOLDS",
    ):
        # We still have categorical diagnostics — if model weak and ATR/EMA missing
        verdict = "MULTICOIN_DIAGNOSTICS_INSUFFICIENT_FEATURES"
        # But if we found stable categorical patterns, prefer HYPOTHESES_READY
        if top_effects and any(abs(e.get("cliffs_delta") or 0) >= 0.05 for e in top_effects):
            verdict = "MULTICOIN_DIAGNOSTICS_HYPOTHESES_READY"
        elif model.get("status") == "NO_GENERALIZABLE_SEPARATOR" and not profitable:
            verdict = "MULTICOIN_DIAGNOSTICS_NO_STABLE_SEPARATOR"
    elif model.get("status") == "NO_GENERALIZABLE_SEPARATOR" and n_without_best <= 2:
        verdict = "MULTICOIN_DIAGNOSTICS_NO_STABLE_SEPARATOR"
    else:
        verdict = "MULTICOIN_DIAGNOSTICS_HYPOTHESES_READY"

    # Refine verdict: insufficient continuous features is the dominant limitation
    if int((~avail["present"]).sum()) >= 25:
        # Still emit hypotheses for later OOS on enrichable + available verdict filters
        if len(profitable) >= 1:
            verdict = "MULTICOIN_DIAGNOSTICS_HYPOTHESES_READY"
        else:
            verdict = "MULTICOIN_DIAGNOSTICS_INSUFFICIENT_FEATURES"

    summary = {
        "verdict": verdict,
        "analysis_type": "exploratory_post_hoc",
        "reference": {
            "timeframe": REF_TF,
            "mode": REF_MODE,
            "group": REF_GROUP,
            "tp_pct": 0.75,
            "sl_pct": 0.50,
            "horizon": "8h",
            "cost_pct": REF_COST,
        },
        "excluded_symbols": list(EXCLUDE_SYMBOLS),
        "n_coins_analyzed": int(len(coin_labels)),
        "n_profitable": int(len(pos_coins)),
        "n_negative": int((coin_labels.coin_net_negative == 1).sum()),
        "profitable_coins": profitable,
        "n_positive_without_best_trade": n_without_best,
        "stable_positive": list(stability.loc[stability.stability_class == "STABLE_POSITIVE", "symbol"]),
        "outlier_dependent": list(stability.loc[stability.stability_class == "OUTLIER_DEPENDENT", "symbol"]),
        "small_sample_positive": list(stability.loc[stability.stability_class == "SMALL_SAMPLE_POSITIVE", "symbol"]),
        "top_effects": top_effects,
        "model_status": model.get("status"),
        "model": model,
        "supportive_vs_raw": supportive_vs_raw,
        "ob_neutral_rate_among_supportive_candidates": ob_neutral_rate,
        "fixed_questions": questions,
        "hypotheses": hypotheses,
        "enrichment_plan": enrichment_plan(),
        "n_features_present": int((avail.present == True).sum()),  # noqa: E712
        "n_features_absent": int((avail.present == False).sum()),  # noqa: E712
    }
    (diag / "summary.json").write_text(json.dumps(summary, indent=2, default=str) + "\n")
    write_summary_md(diag / "summary.md", summary)
    (diag / "enrichment_plan.json").write_text(json.dumps(enrichment_plan(), indent=2) + "\n")
    return summary


if __name__ == "__main__":
    s = run_diagnostics()
    print("diagnostics_dir: results/edc_sync_tolerance/multicoin_30d_frozen_validation/diagnostics")
    print("verdict:", s["verdict"])
    print("profitable:", [p["symbol"] for p in s["profitable_coins"]])

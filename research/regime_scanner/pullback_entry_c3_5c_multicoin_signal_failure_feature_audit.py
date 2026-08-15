"""Multicoin winner/loser audit for frozen H1–H3 hypotheses (diagnostic only)."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any, Sequence

import numpy as np
import pandas as pd

from research.regime_scanner.point_audit import json_safe
from research.regime_scanner.trend_weakening_multi_bar_audit import assert_safe_output_dir

DEFAULT_STORE = Path("research/regime_scanner/results/multicoin_signal_feature_store_20260722")
DEFAULT_OUT = Path("research/regime_scanner/results/multicoin_signal_feature_audit_20260722")

# Frozen primary hypotheses (trigger-stage features)
H1 = "entry_candle_body_pct"
H2 = "breakout_candle_atr"  # == breakout_candle_range_atr
H3 = "volume_ratio"  # median20 ratio
PRIMARY = {
    "H1_entry_candle_body": H1,
    "H2_breakout_range_atr": H2,
    "H3_volume_ratio": H3,
}
EXTRA_H = [
    "body_to_range_ratio",
    "signed_body_in_trade_direction",
    "breakout_extension_beyond_level_atr",
    "volume_ratio_mean20",
    "volume_zscore",
]


def _cohens_d(a: np.ndarray, b: np.ndarray) -> float | None:
    a = a[np.isfinite(a)]
    b = b[np.isfinite(b)]
    if len(a) < 2 or len(b) < 2:
        return None
    va, vb = float(np.var(a, ddof=1)), float(np.var(b, ddof=1))
    pooled = math.sqrt(((len(a) - 1) * va + (len(b) - 1) * vb) / max(1, len(a) + len(b) - 2))
    if pooled < 1e-15:
        return 0.0
    return float((np.mean(a) - np.mean(b)) / pooled)


def _mannwhitney_p(a: np.ndarray, b: np.ndarray) -> float | None:
    try:
        from scipy.stats import mannwhitneyu
    except ImportError:
        return None
    a = a[np.isfinite(a)]
    b = b[np.isfinite(b)]
    if len(a) < 2 or len(b) < 2:
        return None
    try:
        return float(mannwhitneyu(a, b, alternative="two-sided").pvalue)
    except Exception:  # noqa: BLE001
        return None


def _spearman(x: pd.Series, y: pd.Series) -> float | None:
    try:
        from scipy.stats import spearmanr
    except ImportError:
        return float(pd.Series(x).corr(pd.Series(y), method="spearman"))
    xx = pd.to_numeric(x, errors="coerce")
    yy = pd.to_numeric(y, errors="coerce")
    m = xx.notna() & yy.notna()
    if m.sum() < 5:
        return None
    r, _ = spearmanr(xx[m], yy[m])
    return None if r is None or (isinstance(r, float) and math.isnan(r)) else float(r)


def load_panel(store_dir: Path) -> pd.DataFrame:
    sigs = pd.read_csv(store_dir / "research_signals_export.csv")
    feats = pd.read_csv(store_dir / "research_signal_features_export.csv")
    outs = pd.read_csv(store_dir / "research_signal_outcomes_export.csv")
    feats_t = feats[feats["feature_stage"] == "trigger"].copy() if "feature_stage" in feats.columns else feats
    # explode feature_json if needed
    if "feature_json" in feats_t.columns:
        extras = []
        for v in feats_t["feature_json"]:
            if isinstance(v, str):
                try:
                    extras.append(json.loads(v))
                except json.JSONDecodeError:
                    extras.append({})
            elif isinstance(v, dict):
                extras.append(v)
            else:
                extras.append({})
        ed = pd.DataFrame(extras)
        for c in ed.columns:
            if c not in feats_t.columns:
                feats_t[c] = ed[c].values
    panel = outs.merge(sigs, on="signal_key", how="left", suffixes=("", "_sig"))
    panel = panel.merge(feats_t, on="signal_key", how="left", suffixes=("", "_feat"))
    if "symbol" not in panel.columns and "symbol_sig" in panel.columns:
        panel["symbol"] = panel["symbol_sig"]
    panel["winner_group"] = np.where(
        panel["net_pnl_pct"] > 0, "winner", np.where(panel["net_pnl_pct"] < 0, "loser", "flat")
    )
    if "side" not in panel.columns and "direction" in panel.columns:
        panel["side"] = panel["direction"]
    # alias H2
    if "breakout_candle_range_atr" in panel.columns and H2 not in panel.columns:
        panel[H2] = panel["breakout_candle_range_atr"]
    if "volume_ratio_median20" in panel.columns:
        panel["volume_ratio"] = panel["volume_ratio"].fillna(panel["volume_ratio_median20"])
    return panel


def effect_row(panel: pd.DataFrame, feature: str, *, slice_name: str) -> dict[str, Any]:
    if feature not in panel.columns:
        return {"slice": slice_name, "feature": feature, "n": int(len(panel)), "missing_rate": 1.0}
    w = pd.to_numeric(panel.loc[panel.winner_group == "winner", feature], errors="coerce")
    l = pd.to_numeric(panel.loc[panel.winner_group == "loser", feature], errors="coerce")
    allv = pd.to_numeric(panel[feature], errors="coerce")
    a, b = w.to_numpy(dtype=float), l.to_numpy(dtype=float)
    return {
        "slice": slice_name,
        "feature": feature,
        "n": int(len(panel)),
        "n_winner": int(np.isfinite(a).sum()),
        "n_loser": int(np.isfinite(b).sum()),
        "missing_rate": float(allv.isna().mean()),
        "winner_mean": float(np.nanmean(a)) if np.isfinite(a).any() else None,
        "loser_mean": float(np.nanmean(b)) if np.isfinite(b).any() else None,
        "winner_median": float(np.nanmedian(a)) if np.isfinite(a).any() else None,
        "loser_median": float(np.nanmedian(b)) if np.isfinite(b).any() else None,
        "winner_p25": float(np.nanpercentile(a[np.isfinite(a)], 25)) if np.isfinite(a).any() else None,
        "winner_p75": float(np.nanpercentile(a[np.isfinite(a)], 75)) if np.isfinite(a).any() else None,
        "loser_p25": float(np.nanpercentile(b[np.isfinite(b)], 25)) if np.isfinite(b).any() else None,
        "loser_p75": float(np.nanpercentile(b[np.isfinite(b)], 75)) if np.isfinite(b).any() else None,
        "mean_diff_w_minus_l": None
        if not (np.isfinite(a).any() and np.isfinite(b).any())
        else float(np.nanmean(a) - np.nanmean(b)),
        "cohens_d": _cohens_d(a, b),
        "mannwhitney_p": _mannwhitney_p(a, b),
        "spearman_net": _spearman(panel[feature], panel["net_pnl_pct"]),
        "spearman_mfe": _spearman(panel[feature], panel["mfe_pct"]) if "mfe_pct" in panel.columns else None,
        "spearman_mae": _spearman(panel[feature], panel["mae_pct"]) if "mae_pct" in panel.columns else None,
        "direction": None
        if not (np.isfinite(a).any() and np.isfinite(b).any())
        else ("winner_higher" if np.nanmean(a) > np.nanmean(b) else "loser_higher"),
    }


def quartile_table(panel: pd.DataFrame, feature: str, *, within_coin: bool) -> pd.DataFrame:
    rows = []
    groups = panel.groupby("symbol") if within_coin else [(None, panel)]
    for sym, g in groups:
        x = pd.to_numeric(g[feature], errors="coerce") if feature in g.columns else pd.Series(dtype=float)
        valid = g.loc[x.notna()].copy()
        if len(valid) < 8:
            continue
        try:
            valid["q"] = pd.qcut(pd.to_numeric(valid[feature], errors="coerce"), 4, labels=["Q1", "Q2", "Q3", "Q4"], duplicates="drop")
        except ValueError:
            continue
        for q, gg in valid.groupby("q"):
            nets = pd.to_numeric(gg["net_pnl_pct"], errors="coerce")
            gains = float(nets[nets > 0].sum())
            losses = float(nets[nets <= 0].sum())
            pf = None if abs(losses) < 1e-15 else gains / abs(losses)
            rows.append(
                {
                    "symbol": sym,
                    "feature": feature,
                    "quartile": str(q),
                    "n": int(len(gg)),
                    "winrate": float((nets > 0).mean()),
                    "net_expectancy": float(nets.mean()),
                    "profit_factor": pf,
                    "tp_rate": float((gg["exit_reason"] == "TP").mean()) if "exit_reason" in gg.columns else None,
                    "sl_rate": float(gg["exit_reason"].isin(["SL", "same_bar_conservative_sl"]).mean())
                    if "exit_reason" in gg.columns
                    else None,
                    "n_long": int((gg["side"] == "long").sum()) if "side" in gg.columns else None,
                    "n_short": int((gg["side"] == "short").sum()) if "side" in gg.columns else None,
                }
            )
    return pd.DataFrame(rows)


def path_types(panel: pd.DataFrame) -> pd.DataFrame:
    """Quantile-based path types (transparent, no trading rule)."""
    out = panel.copy()
    mae = pd.to_numeric(out.get("mae_pct"), errors="coerce")
    mfe = pd.to_numeric(out.get("mfe_pct"), errors="coerce")
    bars_tp = pd.to_numeric(out.get("bars_to_tp"), errors="coerce")
    bars_sl = pd.to_numeric(out.get("bars_to_sl"), errors="coerce")
    held = pd.to_numeric(out.get("bars_held"), errors="coerce")
    mae_q = mae.quantile(0.5)
    mfe_q = mfe.quantile(0.5)
    held_q = held.quantile(0.5)
    types = []
    for i, row in out.iterrows():
        er = row.get("exit_reason")
        mae_i = row.get("mae_pct")
        mfe_i = row.get("mfe_pct")
        held_i = row.get("bars_held")
        if er == "time_exit" or er == "data_end":
            types.append("time_exit")
        elif er == "TP":
            if pd.notna(mae_i) and abs(float(mae_i)) <= abs(float(mae_q)) if pd.notna(mae_q) else True:
                types.append("direct_winner")
            else:
                types.append("reclaim_winner")
        elif er in ("SL", "same_bar_conservative_sl"):
            if pd.notna(held_i) and pd.notna(held_q) and float(held_i) <= float(held_q):
                types.append("immediate_loser")
            else:
                types.append("delayed_loser")
        else:
            types.append("other")
    out["path_type"] = types
    out["path_thresholds"] = json.dumps({"mae_median": None if pd.isna(mae_q) else float(mae_q), "held_median": None if pd.isna(held_q) else float(held_q)})
    return out


def generalization_flags(by_coin: pd.DataFrame, feature: str, *, apt_excluded: pd.DataFrame | None = None) -> dict[str, Any]:
    sub = by_coin[by_coin.feature == feature].dropna(subset=["direction"])
    if sub.empty:
        return {"feature": feature, "generalizing": False, "reason": "no_coin_effects"}
    dirs = sub["direction"].value_counts()
    top_dir = dirs.index[0]
    share = float(dirs.iloc[0] / dirs.sum())
    without_apt = sub[sub.symbol != "APTUSDT"]
    share_wo = None
    if len(without_apt):
        d2 = without_apt["direction"].value_counts()
        share_wo = float(d2.iloc[0] / d2.sum()) if len(d2) else None
    ok = (
        share >= 0.60
        and (share_wo is None or share_wo >= 0.60)
        and abs(float(sub["cohens_d"].median(skipna=True) or 0)) >= 0.15
    )
    return {
        "feature": feature,
        "dominant_direction": top_dir,
        "pct_coins_same_direction": share,
        "pct_coins_same_direction_without_apt": share_wo,
        "median_coin_cohens_d": None if sub["cohens_d"].dropna().empty else float(sub["cohens_d"].median()),
        "n_coins": int(len(sub)),
        "generalizing_candidate": bool(ok),
        "note": "requires >=60% coin direction agreement and |median coin d|>=0.15; diagnostic only",
    }


def run_audit(*, store_dir: Path, output_dir: Path) -> dict[str, Any]:
    assert_safe_output_dir(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    panel = load_panel(store_dir)
    features = list(PRIMARY.values()) + [f for f in EXTRA_H if f in panel.columns]

    global_rows = [effect_row(panel, f, slice_name="all") for f in features]
    side_rows = []
    for side in ("long", "short"):
        g = panel[panel["side"] == side]
        for f in features:
            side_rows.append(effect_row(g, f, slice_name=side))
    coin_rows = []
    for sym, g in panel.groupby("symbol"):
        for f in features:
            r = effect_row(g, f, slice_name=f"coin:{sym}")
            r["symbol"] = sym
            coin_rows.append(r)
    split_rows = []
    if "split" in panel.columns:
        for sp, g in panel.groupby("split"):
            for f in features:
                split_rows.append(effect_row(g, f, slice_name=f"split:{sp}"))
    month_rows = []
    if "month" in panel.columns:
        for m, g in panel.groupby("month"):
            for f in list(PRIMARY.values()):
                month_rows.append(effect_row(g, f, slice_name=f"month:{m}"))

    # common window: Jan26–Jun28 where available
    cw = panel.copy()
    if "fill_timestamp" in cw.columns:
        ft = pd.to_datetime(cw["fill_timestamp"], utc=True)
        cw = cw[(ft >= pd.Timestamp("2026-01-26", tz="UTC")) & (ft < pd.Timestamp("2026-06-28", tz="UTC"))]
    cw_rows = [effect_row(cw, f, slice_name="common_window") for f in features]
    without_apt = panel[panel.symbol != "APTUSDT"]
    wo_apt_rows = [effect_row(without_apt, f, slice_name="without_apt") for f in features]

    # without top3 by |sum net|
    coin_pnl = panel.groupby("symbol")["net_pnl_pct"].sum().sort_values(key=lambda s: s.abs(), ascending=False)
    top3 = list(coin_pnl.head(3).index)
    without_top3 = panel[~panel.symbol.isin(top3)]
    wo_top3_rows = [effect_row(without_top3, f, slice_name="without_top3") for f in features]

    # equal-coin: mean of per-coin mean diffs
    by_coin_df = pd.DataFrame(coin_rows)
    equal_rows = []
    for f in features:
        sub = by_coin_df[by_coin_df.feature == f].dropna(subset=["mean_diff_w_minus_l"])
        equal_rows.append(
            {
                "feature": f,
                "n_coins": int(len(sub)),
                "mean_of_coin_mean_diffs": float(sub["mean_diff_w_minus_l"].mean()) if len(sub) else None,
                "median_of_coin_mean_diffs": float(sub["mean_diff_w_minus_l"].median()) if len(sub) else None,
                "pct_coins_winner_higher": float((sub["direction"] == "winner_higher").mean()) if len(sub) else None,
            }
        )

    # quartiles
    q_global = pd.concat([quartile_table(panel, f, within_coin=False) for f in PRIMARY.values()], ignore_index=True)
    q_coin = pd.concat([quartile_table(panel, f, within_coin=True) for f in PRIMARY.values()], ignore_index=True)

    # path types
    typed = path_types(panel)
    typed[["signal_key", "symbol", "side", "exit_reason", "net_pnl_pct", "mfe_pct", "mae_pct", "bars_held", "path_type"]].to_csv(
        output_dir / "signal_path_types.csv", index=False
    )
    path_rows = []
    for pt, g in typed.groupby("path_type"):
        for f in PRIMARY.values():
            path_rows.append(effect_row(g, f, slice_name=f"path:{pt}"))

    gen = [generalization_flags(by_coin_df, f) for f in PRIMARY.values()]

    # hypothesis summary
    hyp_rows = []
    for hid, feat in PRIMARY.items():
        g = next(r for r in global_rows if r["feature"] == feat)
        gf = next(x for x in gen if x["feature"] == feat)
        hyp_rows.append(
            {
                "hypothesis": hid,
                "feature": feat,
                "global_cohens_d": g.get("cohens_d"),
                "global_direction": g.get("direction"),
                "spearman_net": g.get("spearman_net"),
                "generalizing_candidate": gf.get("generalizing_candidate"),
                "pct_coins_same_direction": gf.get("pct_coins_same_direction"),
                "pct_without_apt": gf.get("pct_coins_same_direction_without_apt"),
                "filter_justified": False,
                "note": "no automatic filter; n large but still diagnostic",
            }
        )

    pd.DataFrame(hyp_rows).to_csv(output_dir / "feature_hypothesis_summary.csv", index=False)
    pd.DataFrame(global_rows).to_csv(output_dir / "feature_effects_global.csv", index=False)
    pd.DataFrame(side_rows).to_csv(output_dir / "feature_effects_by_side.csv", index=False)
    by_coin_df.to_csv(output_dir / "feature_effects_by_coin.csv", index=False)
    pd.DataFrame(split_rows).to_csv(output_dir / "feature_effects_by_split.csv", index=False)
    pd.DataFrame(month_rows).to_csv(output_dir / "feature_effects_by_month.csv", index=False)
    q_global.to_csv(output_dir / "feature_quartiles_global.csv", index=False)
    q_coin.to_csv(output_dir / "feature_quartiles_within_coin.csv", index=False)
    pd.DataFrame(cw_rows).to_csv(output_dir / "feature_common_window.csv", index=False)
    pd.DataFrame(wo_apt_rows).to_csv(output_dir / "feature_without_apt.csv", index=False)
    pd.DataFrame(wo_top3_rows).to_csv(output_dir / "feature_without_top3.csv", index=False)
    pd.DataFrame(equal_rows).to_csv(output_dir / "feature_equal_coin_summary.csv", index=False)
    pd.DataFrame(path_rows).to_csv(output_dir / "feature_effects_by_path_type.csv", index=False)

    dist = panel["winner_group"].value_counts().to_dict()
    report = [
        "# Multicoin Signal Feature Audit (H1–H3)",
        "",
        f"- n=`{len(panel)}` · winners/losers=`{dist}` · top3 excluded coins=`{top3}`",
        "- Primary hypotheses frozen: Entry-Candle-Body, Breakout-Range/ATR, Volume-Ratio",
        "- **No filter activated. No A6/Pine change.**",
        "",
        "## Hypothesis summary",
        "",
        "```",
        pd.DataFrame(hyp_rows).to_string(index=False),
        "```",
        "",
        "## Equal-coin",
        "",
        "```",
        pd.DataFrame(equal_rows).to_string(index=False),
        "```",
        "",
        "## Generalization",
        "",
        "```",
        pd.DataFrame(gen).to_string(index=False),
        "```",
        "",
    ]
    (output_dir / "multicoin_signal_feature_report.md").write_text("\n".join(report) + "\n", encoding="utf-8")

    any_gen = any(bool(h.get("generalizing_candidate")) for h in hyp_rows)
    meta = {
        "ok": True,
        "n": int(len(panel)),
        "distribution": {str(k): int(v) for k, v in dist.items()},
        "primary_features": PRIMARY,
        "hypotheses": hyp_rows,
        "generalization": gen,
        "top3_excluded": top3,
        "any_generalizing_candidate": bool(any_gen),
        "filter_candidate_justifies_further_falsification": bool(any_gen),
        "no_filter_activated": True,
        "a6_unchanged": True,
        "pine_unchanged": True,
    }
    (output_dir / "metadata.json").write_text(json.dumps(json_safe(meta), indent=2) + "\n", encoding="utf-8")
    return meta


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Multicoin H1–H3 feature failure audit")
    p.add_argument("--store-dir", type=Path, default=DEFAULT_STORE)
    p.add_argument("--output-dir", type=Path, default=DEFAULT_OUT)
    p.add_argument("--run-label", default="multicoin_a6_signal_store_20260722")
    p.add_argument("--regime-db-env", type=Path, default=None)
    args = p.parse_args(list(argv) if argv is not None else None)
    meta = run_audit(store_dir=args.store_dir, output_dir=args.output_dir)
    print(json.dumps(json_safe({"ok": meta.get("ok"), "n": meta.get("n"), "gen": meta.get("any_generalizing_candidate")})))
    return 0 if meta.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())

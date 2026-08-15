"""Winner/Loser feature diagnostic audit for persisted (or exported) A6 signals.

No filter activation. n=55 — prioritize effect sizes, not p-values.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any, Sequence

import numpy as np
import pandas as pd

from research.regime_scanner.candle_sources import load_regime_db_env_file
from research.regime_scanner.c35c_signal_store.store import C35cSignalStore
from research.regime_scanner.mysql_candle_store.config import load_regime_db_config
from research.regime_scanner.point_audit import json_safe
from research.regime_scanner.trend_weakening_multi_bar_audit import assert_safe_output_dir

DEFAULT_OUT = Path("research/regime_scanner/results/apt_signal_feature_store_20260722")
DEFAULT_ENV = Path(
    "/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/"
    "research/regime_scanner/.env.regime_db"
)

NUMERIC_FEATURE_COLS = [
    "setup_age",
    "ready_age",
    "bars_arm_to_trigger",
    "bars_ready_to_trigger",
    "opposite_arm_age",
    "adx",
    "di_spread_dir_norm",
    "di_spread_abs",
    "ema9_20_distance_pct",
    "dist_ema_atr",
    "move_since_arm_atr",
    "breakout_candle_atr",
    "pullback_depth_atr",
    "entry_candle_return_pct",
    "entry_candle_body_pct",
    "entry_upper_wick_ratio",
    "entry_lower_wick_ratio",
    "volume_ratio",
    "atr_pct",
    "mfe_pct",
    "mae_pct",
    "bars_held",
]

CATEGORICAL_COLS = [
    "side",
    "split",
    "exit_reason",
    "opposite_arm_seen",
    "opposite_arm_since_ready",
    "opposite_arm_on_trigger_bar",
    "structure_state",
    "major_direction",
    "entry_bullish",
]


def _cohens_d(a: np.ndarray, b: np.ndarray) -> float | None:
    a = a[np.isfinite(a)]
    b = b[np.isfinite(b)]
    if len(a) < 2 or len(b) < 2:
        return None
    va, vb = float(np.var(a, ddof=1)), float(np.var(b, ddof=1))
    pooled = math.sqrt(((len(a) - 1) * va + (len(b) - 1) * vb) / (len(a) + len(b) - 2))
    if pooled < 1e-15:
        return 0.0
    return float((np.mean(a) - np.mean(b)) / pooled)


def _mannwhitney_u(a: np.ndarray, b: np.ndarray) -> float | None:
    try:
        from scipy.stats import mannwhitneyu
    except ImportError:
        return None
    a = a[np.isfinite(a)]
    b = b[np.isfinite(b)]
    if len(a) < 2 or len(b) < 2:
        return None
    try:
        res = mannwhitneyu(a, b, alternative="two-sided")
        return float(res.pvalue)
    except Exception:  # noqa: BLE001
        return None


def load_joined_panel(
    *,
    run_label: str | None,
    regime_db_env: Path,
    output_dir: Path,
    prefer_csv: bool = True,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    sig_csv = output_dir / "research_signals_export.csv"
    feat_csv = output_dir / "research_signal_features_export.csv"
    out_csv = output_dir / "research_signal_outcomes_export.csv"
    meta: dict[str, Any] = {"source": None}

    if prefer_csv and sig_csv.exists() and feat_csv.exists() and out_csv.exists():
        sigs = pd.read_csv(sig_csv)
        feats = pd.read_csv(feat_csv)
        outs = pd.read_csv(out_csv)
        meta["source"] = "csv_export"
    else:
        load_regime_db_env_file(regime_db_env)
        store = C35cSignalStore(load_regime_db_config())
        try:
            store.init_schema()
            if not run_label:
                raise ValueError("run_label required for MySQL load")
            run = store.find_completed_run_by_label(run_label)
            if run is None:
                raise RuntimeError(f"no completed run for label={run_label}")
            sig_rows = store.load_signals(run["run_id"])
            feat_rows = store.load_features(run["run_id"])
            out_rows = store.load_outcomes(run["run_id"])
            meta["source"] = "mysql"
            meta["run_id"] = run["run_id"]
            sigs = pd.DataFrame(
                [
                    {
                        "signal_key": s["signal_key"],
                        "side": s["direction"],
                        "setup_id": s["setup_id"],
                        "trigger_timestamp": s["timestamp"],
                        "fill_timestamp": s["entry_time"],
                        "entry_price": s["entry_price"],
                        **(json.loads(s["metadata_json"]) if isinstance(s.get("metadata_json"), str) else (s.get("metadata_json") or {})),
                    }
                    for s in sig_rows
                ]
            )
            feats = pd.DataFrame(feat_rows)
            outs = pd.DataFrame(out_rows)
        finally:
            store.close()

    # trigger-stage features for diagnostics
    if "feature_stage" in feats.columns:
        feats_t = feats[feats["feature_stage"] == "trigger"].copy()
    else:
        feats_t = feats.copy()

    # explode feature_json extras if present as string
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
        extra_df = pd.DataFrame(extras)
        for c in extra_df.columns:
            if c not in feats_t.columns:
                feats_t[c] = extra_df[c].values

    panel = outs.merge(sigs, on="signal_key", how="left", suffixes=("", "_sig"))
    panel = panel.merge(feats_t, on="signal_key", how="left", suffixes=("", "_feat"))
    panel["winner_group"] = np.where(
        panel["net_pnl_pct"] > 0, "winner", np.where(panel["net_pnl_pct"] < 0, "loser", "flat")
    )
    if "side" not in panel.columns and "direction" in panel.columns:
        panel["side"] = panel["direction"]
    meta["n"] = int(len(panel))
    return panel, meta


def compare_numeric(panel: pd.DataFrame) -> pd.DataFrame:
    rows = []
    winners = panel[panel["winner_group"] == "winner"]
    losers = panel[panel["winner_group"] == "loser"]
    for col in NUMERIC_FEATURE_COLS:
        if col not in panel.columns:
            continue
        w = pd.to_numeric(winners[col], errors="coerce")
        l = pd.to_numeric(losers[col], errors="coerce")
        a = w.to_numpy(dtype=float)
        b = l.to_numpy(dtype=float)
        rows.append(
            {
                "feature": col,
                "n_winner": int(np.isfinite(a).sum()),
                "n_loser": int(np.isfinite(b).sum()),
                "missing_rate": float(pd.to_numeric(panel[col], errors="coerce").isna().mean()),
                "winner_mean": float(np.nanmean(a)) if np.isfinite(a).any() else None,
                "loser_mean": float(np.nanmean(b)) if np.isfinite(b).any() else None,
                "winner_median": float(np.nanmedian(a)) if np.isfinite(a).any() else None,
                "loser_median": float(np.nanmedian(b)) if np.isfinite(b).any() else None,
                "winner_p25": float(np.nanpercentile(a[np.isfinite(a)], 25)) if np.isfinite(a).any() else None,
                "winner_p75": float(np.nanpercentile(a[np.isfinite(a)], 75)) if np.isfinite(a).any() else None,
                "loser_p25": float(np.nanpercentile(b[np.isfinite(b)], 25)) if np.isfinite(b).any() else None,
                "loser_p75": float(np.nanpercentile(b[np.isfinite(b)], 75)) if np.isfinite(b).any() else None,
                "mean_diff_winner_minus_loser": (
                    None
                    if not (np.isfinite(a).any() and np.isfinite(b).any())
                    else float(np.nanmean(a) - np.nanmean(b))
                ),
                "cohens_d": _cohens_d(a, b),
                "mannwhitney_p": _mannwhitney_u(a, b),
            }
        )
    return pd.DataFrame(rows).sort_values("cohens_d", key=lambda s: s.abs(), ascending=False)


def compare_categorical(panel: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for col in CATEGORICAL_COLS:
        if col not in panel.columns:
            continue
        for val, g in panel.groupby(col, dropna=False):
            nets = pd.to_numeric(g["net_pnl_pct"], errors="coerce")
            rows.append(
                {
                    "feature": col,
                    "value": val,
                    "n": int(len(g)),
                    "share": float(len(g) / len(panel)) if len(panel) else None,
                    "winrate": float((nets > 0).mean()) if len(g) else None,
                    "net_expectancy": float(nets.mean()) if len(g) else None,
                    "sum_pp": float(nets.sum()) if len(g) else None,
                }
            )
    return pd.DataFrame(rows)


def by_side(panel: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for side, g in panel.groupby("side"):
        for wg, gg in g.groupby("winner_group"):
            rows.append(
                {
                    "side": side,
                    "winner_group": wg,
                    "n": int(len(gg)),
                    "share_of_side": float(len(gg) / len(g)),
                    "net_expectancy": float(pd.to_numeric(gg["net_pnl_pct"], errors="coerce").mean()),
                }
            )
        # key feature means
        for col in ("adx", "bars_arm_to_trigger", "di_spread_dir_norm", "move_since_arm_atr", "pullback_depth_atr"):
            if col not in g.columns:
                continue
            w = g[g.winner_group == "winner"]
            l = g[g.winner_group == "loser"]
            rows.append(
                {
                    "side": side,
                    "winner_group": f"mean_diff::{col}",
                    "n": int(len(g)),
                    "share_of_side": None,
                    "net_expectancy": float(
                        pd.to_numeric(w[col], errors="coerce").mean()
                        - pd.to_numeric(l[col], errors="coerce").mean()
                    )
                    if len(w) and len(l)
                    else None,
                }
            )
    return pd.DataFrame(rows)


def by_split(panel: pd.DataFrame) -> pd.DataFrame:
    rows = []
    if "split" not in panel.columns:
        return pd.DataFrame()
    for sp, g in panel.groupby("split"):
        nets = pd.to_numeric(g["net_pnl_pct"], errors="coerce")
        rows.append(
            {
                "split": sp,
                "n": int(len(g)),
                "n_winner": int((nets > 0).sum()),
                "n_loser": int((nets < 0).sum()),
                "winrate": float((nets > 0).mean()),
                "net_expectancy": float(nets.mean()),
            }
        )
        for col in ("adx", "bars_arm_to_trigger", "opposite_arm_seen", "di_spread_dir_norm"):
            if col not in g.columns:
                continue
            w = g[g.winner_group == "winner"]
            l = g[g.winner_group == "loser"]
            if col == "opposite_arm_seen":
                rows.append(
                    {
                        "split": sp,
                        "n": int(len(g)),
                        "n_winner": None,
                        "n_loser": None,
                        "winrate": None,
                        "net_expectancy": None,
                        "feature": col,
                        "winner_rate": float(pd.to_numeric(w[col], errors="coerce").fillna(0).astype(bool).mean())
                        if len(w)
                        else None,
                        "loser_rate": float(pd.to_numeric(l[col], errors="coerce").fillna(0).astype(bool).mean())
                        if len(l)
                        else None,
                    }
                )
            else:
                rows.append(
                    {
                        "split": sp,
                        "feature": col,
                        "n": int(len(g)),
                        "n_winner": int(len(w)),
                        "n_loser": int(len(l)),
                        "winrate": None,
                        "net_expectancy": float(
                            pd.to_numeric(w[col], errors="coerce").mean()
                            - pd.to_numeric(l[col], errors="coerce").mean()
                        )
                        if len(w) and len(l)
                        else None,
                    }
                )
    return pd.DataFrame(rows)


def diagnose_hypotheses(num: pd.DataFrame, cat: pd.DataFrame, panel: pd.DataFrame) -> list[dict[str, Any]]:
    def _num(feat: str) -> dict[str, Any] | None:
        r = num[num.feature == feat]
        return None if r.empty else r.iloc[0].to_dict()

    def _effect_ok(r: dict[str, Any] | None, *, min_abs_d: float = 0.25) -> bool:
        if not r:
            return False
        d = r.get("cohens_d")
        return d is not None and abs(float(d)) >= min_abs_d

    hyps = []
    # 1 older at trigger?
    r = _num("bars_arm_to_trigger") or _num("setup_age")
    hyps.append(
        {
            "id": 1,
            "question": "Are losers older at trigger?",
            "plausible": bool(
                _effect_ok(r) and r.get("mean_diff_winner_minus_loser") is not None and r["mean_diff_winner_minus_loser"] < 0
            ),
            "evidence": r,
        }
    )
    # 2 opposite arm since ready
    if "opposite_arm_since_ready" in panel.columns:
        w = panel[panel.winner_group == "winner"]["opposite_arm_since_ready"]
        l = panel[panel.winner_group == "loser"]["opposite_arm_since_ready"]
        wr = float(pd.to_numeric(w, errors="coerce").fillna(0).astype(bool).mean()) if len(w) else None
        lr = float(pd.to_numeric(l, errors="coerce").fillna(0).astype(bool).mean()) if len(l) else None
        hyps.append(
            {
                "id": 2,
                "question": "Do losers more often have opposite arm since ready?",
                "plausible": bool(lr is not None and wr is not None and lr > wr + 0.05),
                "evidence": {"winner_rate": wr, "loser_rate": lr},
            }
        )
    else:
        hyps.append({"id": 2, "question": "opposite arm since ready", "plausible": False, "evidence": "missing"})

    for hid, feat, q, loser_weaker_if_diff_pos in (
        (3, "adx", "Do losers have weaker ADX?", True),
        (4, "di_spread_dir_norm", "Do losers have smaller dir-normalized DI spread?", True),
        (5, "ema9_20_distance_pct", "Are losers farther from EMA9/20?", False),
        (6, "move_since_arm_atr", "Is move since arming larger for losers?", False),
        (7, "breakout_candle_atr", "Is breakout candle larger for losers?", False),
        (8, "pullback_depth_atr", "Is pullback deeper for losers?", False),
        (9, "entry_candle_body_pct", "Does entry candle body differ?", None),
    ):
        r = _num(feat)
        if r is None or r.get("mean_diff_winner_minus_loser") is None or not _effect_ok(r):
            hyps.append({"id": hid, "question": q, "plausible": False, "evidence": r or "insufficient"})
            continue
        diff = r["mean_diff_winner_minus_loser"]
        d = r.get("cohens_d")
        if loser_weaker_if_diff_pos is True:
            plausible = diff > 0  # winner > loser
        elif loser_weaker_if_diff_pos is False:
            # "larger for losers" → winner-loser diff < 0
            # special-case H5: farther = larger abs distance for losers
            if feat == "ema9_20_distance_pct":
                w_abs = abs(float(r.get("winner_mean") or 0))
                l_abs = abs(float(r.get("loser_mean") or 0))
                plausible = l_abs > w_abs
            else:
                plausible = diff < 0
        else:
            plausible = bool(d is not None and abs(d) >= 0.3)
        hyps.append({"id": hid, "question": q, "plausible": bool(plausible), "evidence": r})

    # 10 long vs short
    if "side" in panel.columns:
        long = panel[panel.side == "long"]
        short = panel[panel.side == "short"]
        hyps.append(
            {
                "id": 10,
                "question": "Do Long and Short differ?",
                "plausible": True,
                "evidence": {
                    "long_n": int(len(long)),
                    "short_n": int(len(short)),
                    "long_E": float(pd.to_numeric(long.net_pnl_pct, errors="coerce").mean()) if len(long) else None,
                    "short_E": float(pd.to_numeric(short.net_pnl_pct, errors="coerce").mean()) if len(short) else None,
                },
            }
        )
    # 11 split alignment
    if "split" in panel.columns:
        parts = []
        for sp, g in panel.groupby("split"):
            r = compare_numeric(g)
            adx = r[r.feature == "adx"]
            parts.append(
                {
                    "split": sp,
                    "adx_diff": None if adx.empty else adx.iloc[0].get("mean_diff_winner_minus_loser"),
                    "adx_d": None if adx.empty else adx.iloc[0].get("cohens_d"),
                }
            )
        signs = [p["adx_diff"] for p in parts if p["adx_diff"] is not None and p.get("adx_d") is not None and abs(p["adx_d"]) >= 0.2]
        aligned = len(signs) >= 2 and (all(s > 0 for s in signs) or all(s < 0 for s in signs))
        hyps.append({"id": 11, "question": "Effects aligned across Dev/Val/OOS?", "plausible": bool(aligned), "evidence": parts})
    # 12 few trades driven?
    hyps.append(
        {
            "id": 12,
            "question": "Are effects driven by few trades?",
            "plausible": True,
            "evidence": {"warning": "n=55 total; any effect may be fragile / overfitting risk"},
        }
    )
    return hyps


def run_failure_feature_audit(
    *,
    run_label: str,
    regime_db_env: Path,
    output_dir: Path,
) -> dict[str, Any]:
    assert_safe_output_dir(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    panel, load_meta = load_joined_panel(
        run_label=run_label, regime_db_env=regime_db_env, output_dir=output_dir, prefer_csv=True
    )
    num = compare_numeric(panel)
    cat = compare_categorical(panel)
    side = by_side(panel)
    split = by_split(panel)
    hyps = diagnose_hypotheses(num, cat, panel)
    effects = num.copy()
    if not effects.empty and "cohens_d" in effects.columns:
        effects["abs_d"] = effects["cohens_d"].abs()
        top = effects.sort_values("abs_d", ascending=False).head(15)
    else:
        top = effects.head(15)

    num.to_csv(output_dir / "winner_loser_numeric_features.csv", index=False)
    cat.to_csv(output_dir / "winner_loser_categorical_features.csv", index=False)
    side.to_csv(output_dir / "winner_loser_by_side.csv", index=False)
    split.to_csv(output_dir / "winner_loser_by_split.csv", index=False)
    effects.to_csv(output_dir / "winner_loser_feature_effects.csv", index=False)
    top.to_csv(output_dir / "top_diagnostic_features.csv", index=False)

    dist = panel["winner_group"].value_counts().to_dict()
    exit_dist = panel["exit_reason"].value_counts().to_dict() if "exit_reason" in panel.columns else {}
    plausible = [h for h in hyps if h.get("plausible")]
    not_supported = [h for h in hyps if not h.get("plausible")]

    report = [
        "# Signal Failure Feature Audit (APT A6)",
        "",
        f"- source: `{load_meta.get('source')}` · n=`{len(panel)}`",
        f"- winner/loser/flat: `{dist}`",
        f"- exit reasons: `{exit_dist}`",
        "",
        "**Warning:** n=55 — do not over-interpret p-values; effect sizes only. No filters activated.",
        "",
        "## Top diagnostic features (|Cohen's d|)",
        "",
        "```",
        top[["feature", "winner_mean", "loser_mean", "mean_diff_winner_minus_loser", "cohens_d", "mannwhitney_p"]].to_string(index=False)
        if not top.empty
        else "(none)",
        "```",
        "",
        "## Hypotheses",
        "",
    ]
    for h in hyps:
        report.append(f"- **H{h['id']}** {'PLausible' if h.get('plausible') else 'Not supported'}: {h['question']}")
    report += [
        "",
        "## Guardrails",
        "",
        "- No filter activation",
        "- No A6 / Pine change",
        "- No ML classifier as strategy",
        "",
    ]
    (output_dir / "signal_failure_feature_report.md").write_text("\n".join(report) + "\n", encoding="utf-8")

    meta = {
        "ok": True,
        "load_meta": load_meta,
        "n": int(len(panel)),
        "distribution": {str(k): int(v) for k, v in dist.items()},
        "exit_distribution": {str(k): int(v) for k, v in exit_dist.items()},
        "hypotheses": hyps,
        "plausible_ids": [h["id"] for h in plausible],
        "not_supported_ids": [h["id"] for h in not_supported],
        "no_filter_activated": True,
        "a6_unchanged": True,
        "pine_unchanged": True,
        "n_warning": "n=55 overfitting risk",
    }
    (output_dir / "failure_audit_metadata.json").write_text(
        json.dumps(json_safe(meta), indent=2) + "\n", encoding="utf-8"
    )
    return meta


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="A6 winner/loser feature diagnostic audit")
    p.add_argument("--run-label", default="apt_a6_signal_store_20260722")
    p.add_argument("--regime-db-env", type=Path, default=DEFAULT_ENV)
    p.add_argument("--output-dir", type=Path, default=DEFAULT_OUT)
    args = p.parse_args(list(argv) if argv is not None else None)
    meta = run_failure_feature_audit(
        run_label=args.run_label,
        regime_db_env=args.regime_db_env,
        output_dir=args.output_dir,
    )
    print(json.dumps(json_safe({"ok": meta.get("ok"), "n": meta.get("n"), "plausible": meta.get("plausible_ids")})))
    return 0 if meta.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())

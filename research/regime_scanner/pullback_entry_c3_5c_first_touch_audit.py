"""C3.5c first-touch audit on real TRIGGER→ENTRY fills (5m/15m). Research-only.

Reuses C3.5c entry-path frame/replay helpers. No SM / Pine / parameter changes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd

from research.regime_scanner.point_audit import json_safe
from research.regime_scanner.pullback_entry_c3_5 import apply_pullback_entry, config_hash
from research.regime_scanner.pullback_entry_c3_5_diagnostics import baseline_a6
from research.regime_scanner.pullback_entry_c3_5_simple_path_audit import collect_filled_entries
from research.regime_scanner.pullback_entry_c3_5c_entry_path_audit import (
    ANALYZE_END,
    ANALYZE_START,
    DEFAULT_BASELINE_DIR,
    TF_MINUTES,
    build_parity_table,
    build_tf_frame,
    horizon_bars_for_tf,
)
from research.regime_scanner.trend_regime_classification_audit import (
    C2_BASELINE_HASH,
    assert_baseline_readonly,
)
from research.regime_scanner.trend_weakening_multi_bar_audit import assert_safe_output_dir

DEFAULT_OUT = Path(
    "research/regime_scanner/results/phase_c3_5_pullback_entry_state_machine/c35c_first_touch_audit"
)

TIMEFRAMES: tuple[str, ...] = ("5m", "15m")
HORIZON_HOURS: tuple[float, ...] = (6, 12, 24, 48, 96, 192)  # 6h..8d
HORIZON_LABELS: tuple[str, ...] = ("6h", "12h", "24h", "48h", "4d", "8d")

# (target_pct, adverse_pct) in signal direction / against signal
TARGET_ADVERSE_COMBOS: tuple[tuple[float, float], ...] = (
    (0.5, 0.5),
    (0.75, 0.5),
    (1.0, 0.5),
    (1.0, 0.75),
    (1.0, 1.0),
    (1.5, 0.75),
    (1.5, 1.0),
    (2.0, 1.0),
    (2.0, 1.5),
    (3.0, 1.5),
    (4.0, 2.0),
    (5.0, 2.5),
)

MONTHS: tuple[str, ...] = ("2026-02", "2026-03", "2026-04")
MIN_DECIDED_FOR_RANK = 20


def horizons_for_tf(timeframe: str) -> list[dict[str, Any]]:
    out = []
    for label, hours in zip(HORIZON_LABELS, HORIZON_HOURS):
        bars, actual = horizon_bars_for_tf(timeframe, hours)
        out.append(
            {
                "horizon_id": f"time_{label}",
                "label": label,
                "target_hours": hours,
                "bars": bars,
                "actual_hours": actual,
            }
        )
    return out


def classify_first_touch(
    *,
    side: int,
    entry_price: float,
    highs: np.ndarray,
    lows: np.ndarray,
    timestamps: Sequence[Any],
    fill_bar: int,
    horizon_bars: int,
    n_bars: int,
    target_pct: float,
    adverse_pct: float,
) -> dict[str, Any]:
    """Walk from fill bar inclusive; same-bar dual hit → ambiguous_same_bar."""
    if entry_price <= 0 or not math.isfinite(entry_price):
        raise ValueError("bad entry_price")
    last_needed = fill_bar + horizon_bars - 1
    incomplete = last_needed >= n_bars
    end_i = min(last_needed, n_bars - 1)
    if fill_bar >= n_bars or end_i < fill_bar:
        return {"outcome": "neither", "valid": False, "incomplete_horizon": True}

    if side > 0:
        target_price = entry_price * (1.0 + target_pct / 100.0)
        adverse_price = entry_price * (1.0 - adverse_pct / 100.0)
    else:
        target_price = entry_price * (1.0 - target_pct / 100.0)
        adverse_price = entry_price * (1.0 + adverse_pct / 100.0)

    fav_hit: int | None = None
    adv_hit: int | None = None

    for loc, j in enumerate(range(fill_bar, end_i + 1)):
        h = float(highs[j])
        l = float(lows[j])
        if side > 0:
            hit_t = h >= target_price
            hit_a = l <= adverse_price
        else:
            hit_t = l <= target_price
            hit_a = h >= adverse_price

        if hit_t and hit_a and fav_hit is None and adv_hit is None:
            return {
                "valid": True,
                "outcome": "ambiguous_same_bar",
                "incomplete_horizon": incomplete,
                "first_touch_timestamp": timestamps[j],
                "bars_to_first_touch": loc,
                "target_price": target_price,
                "adverse_price": adverse_price,
                "same_bar_target_hit": True,
                "same_bar_adverse_hit": True,
                "bars_to_target": loc,
                "bars_to_adverse": loc,
            }
        if hit_t and fav_hit is None:
            fav_hit = loc
        if hit_a and adv_hit is None:
            adv_hit = loc
        if fav_hit is not None and adv_hit is not None:
            break

    if fav_hit is not None and adv_hit is None:
        outcome = "target_first"
        touch_bars = fav_hit
    elif adv_hit is not None and fav_hit is None:
        outcome = "adverse_first"
        touch_bars = adv_hit
    elif fav_hit is not None and adv_hit is not None:
        if fav_hit < adv_hit:
            outcome = "target_first"
            touch_bars = fav_hit
        elif adv_hit < fav_hit:
            outcome = "adverse_first"
            touch_bars = adv_hit
        else:
            outcome = "ambiguous_same_bar"
            touch_bars = fav_hit
    else:
        outcome = "neither"
        touch_bars = None

    touch_ts = timestamps[fill_bar + touch_bars] if touch_bars is not None else None
    return {
        "valid": True,
        "outcome": outcome,
        "incomplete_horizon": incomplete,
        "first_touch_timestamp": touch_ts,
        "bars_to_first_touch": touch_bars,
        "target_price": target_price,
        "adverse_price": adverse_price,
        "same_bar_target_hit": outcome == "ambiguous_same_bar",
        "same_bar_adverse_hit": outcome == "ambiguous_same_bar",
        "bars_to_target": fav_hit,
        "bars_to_adverse": adv_hit,
    }


def build_cases(
    frame: pd.DataFrame,
    entries: Sequence[Mapping[str, Any]],
    *,
    timeframe: str,
    variant: str,
) -> pd.DataFrame:
    n = len(frame)
    highs = frame["high"].astype(float).to_numpy()
    lows = frame["low"].astype(float).to_numpy()
    timestamps = list(frame["timestamp"])
    bar_hours = TF_MINUTES[timeframe] / 60.0
    filled = collect_filled_entries(entries, n)
    horizons = horizons_for_tf(timeframe)
    rows: list[dict[str, Any]] = []
    for e in filled:
        fill_i = int(e["fill_bar"])
        side = int(e["side"])
        entry_px = float(e["entry_price"])
        fill_ts = timestamps[fill_i]
        month = pd.Timestamp(fill_ts).tz_convert("UTC").strftime("%Y-%m")
        for hz in horizons:
            for target_pct, adverse_pct in TARGET_ADVERSE_COMBOS:
                ft = classify_first_touch(
                    side=side,
                    entry_price=entry_px,
                    highs=highs,
                    lows=lows,
                    timestamps=timestamps,
                    fill_bar=fill_i,
                    horizon_bars=int(hz["bars"]),
                    n_bars=n,
                    target_pct=target_pct,
                    adverse_pct=adverse_pct,
                )
                if not ft.get("valid"):
                    continue
                touch_bars = ft.get("bars_to_first_touch")
                rows.append(
                    {
                        "symbol": frame["symbol"].iloc[0],
                        "timeframe": timeframe,
                        "variant": variant,
                        "side": e["side_name"],
                        "setup_id": e.get("setup_id"),
                        "trigger_timestamp": e.get("trigger_timestamp"),
                        "fill_timestamp": fill_ts,
                        "fill_month": month,
                        "entry_price": entry_px,
                        "target_pct": target_pct,
                        "adverse_pct": adverse_pct,
                        "combo_id": f"t{target_pct:g}_a{adverse_pct:g}",
                        "horizon": hz["label"],
                        "horizon_bars": int(hz["bars"]),
                        "horizon_actual_hours": hz["actual_hours"],
                        "outcome": ft["outcome"],
                        "first_touch_timestamp": ft.get("first_touch_timestamp"),
                        "bars_to_first_touch": touch_bars,
                        "hours_to_first_touch": (
                            float(touch_bars) * bar_hours if touch_bars is not None else None
                        ),
                        "target_price": ft["target_price"],
                        "adverse_price": ft["adverse_price"],
                        "same_bar_target_hit": ft["same_bar_target_hit"],
                        "same_bar_adverse_hit": ft["same_bar_adverse_hit"],
                        "bars_to_target": ft.get("bars_to_target"),
                        "bars_to_adverse": ft.get("bars_to_adverse"),
                        "hours_to_target": (
                            float(ft["bars_to_target"]) * bar_hours
                            if ft.get("bars_to_target") is not None
                            else None
                        ),
                        "hours_to_adverse": (
                            float(ft["bars_to_adverse"]) * bar_hours
                            if ft.get("bars_to_adverse") is not None
                            else None
                        ),
                        "incomplete_horizon": bool(ft["incomplete_horizon"]),
                        "conservative_as_adverse": ft["outcome"]
                        in {"adverse_first", "ambiguous_same_bar"},
                        "conservative_as_win": ft["outcome"] == "target_first",
                    }
                )
    return pd.DataFrame(rows)


def _agg_metrics(g: pd.DataFrame) -> dict[str, Any]:
    n = len(g)
    if n == 0:
        return {"n_entries": 0}
    oc = g["outcome"].value_counts()
    n_tf = int(oc.get("target_first", 0))
    n_af = int(oc.get("adverse_first", 0))
    n_amb = int(oc.get("ambiguous_same_bar", 0))
    n_nei = int(oc.get("neither", 0))
    n_decided = n_tf + n_af  # unambiguous only
    n_cons_den = n_tf + n_af + n_amb  # decided + ambiguous (neither excluded)
    cons_wins = n_tf
    cons_win_rate = cons_wins / n_cons_den if n_cons_den else None
    clean_win_rate = n_tf / n_decided if n_decided else None

    target_pct = float(g["target_pct"].iloc[0])
    adverse_pct = float(g["adverse_pct"].iloc[0])
    cons_exp = None
    if cons_win_rate is not None:
        cons_exp = cons_win_rate * target_pct - (1.0 - cons_win_rate) * adverse_pct
    clean_exp = None
    if clean_win_rate is not None:
        clean_exp = clean_win_rate * target_pct - (1.0 - clean_win_rate) * adverse_pct

    def _med(mask_col: str, value_col: str) -> float | None:
        s = g.loc[g["outcome"] == mask_col, value_col].dropna()
        return float(s.median()) if len(s) else None

    return {
        "n_entries": n,
        "target_first_count": n_tf,
        "target_first_rate": n_tf / n,
        "adverse_first_count": n_af,
        "adverse_first_rate": n_af / n,
        "ambiguous_count": n_amb,
        "ambiguous_rate": n_amb / n,
        "neither_count": n_nei,
        "neither_rate": n_nei / n,
        "n_decided": n_decided,
        "n_conservative_denom": n_cons_den,
        "conservative_win_rate": cons_win_rate,
        "clean_win_rate_decided_only": clean_win_rate,
        "conservative_expectancy_pct": cons_exp,
        "clean_expectancy_pct_decided_only": clean_exp,
        "median_hours_to_target": _med("target_first", "hours_to_target"),
        "median_hours_to_adverse": _med("adverse_first", "hours_to_adverse"),
        "median_hours_to_first_touch": float(g["hours_to_first_touch"].dropna().median())
        if g["hours_to_first_touch"].notna().any()
        else None,
        "target_pct": target_pct,
        "adverse_pct": adverse_pct,
    }


def build_summary(cases: pd.DataFrame) -> pd.DataFrame:
    if cases.empty:
        return pd.DataFrame()
    keys = ["timeframe", "side", "horizon", "combo_id", "target_pct", "adverse_pct"]
    rows = []
    for gkeys, g in cases.groupby(keys, dropna=False):
        row = dict(zip(keys, gkeys if isinstance(gkeys, tuple) else (gkeys,)))
        row.update(_agg_metrics(g))
        rows.append(row)
    return pd.DataFrame(rows)


def build_by_month(cases: pd.DataFrame) -> pd.DataFrame:
    if cases.empty:
        return pd.DataFrame()
    keys = ["timeframe", "side", "horizon", "combo_id", "target_pct", "adverse_pct", "fill_month"]
    rows = []
    for gkeys, g in cases.groupby(keys, dropna=False):
        row = dict(zip(keys, gkeys if isinstance(gkeys, tuple) else (gkeys,)))
        row.update(_agg_metrics(g))
        rows.append(row)
    return pd.DataFrame(rows)


def _stability_flag(month_exps: Mapping[str, float | None]) -> str:
    vals = [month_exps.get(m) for m in MONTHS]
    present = [v for v in vals if v is not None]
    if len(present) < 2:
        return "insufficient_months"
    signs = [1 if v > 0 else (-1 if v < 0 else 0) for v in present]
    if len(set(signs)) > 1 and 0 not in set(signs):
        return "unstable_sign_flip"
    # one month dominates magnitude
    abs_vals = [abs(v) for v in present]
    if sum(abs_vals) > 0 and max(abs_vals) / sum(abs_vals) >= 0.7:
        return "unstable_one_month_dominates"
    if all(v > 0 for v in present):
        return "stable_positive"
    if all(v < 0 for v in present):
        return "stable_negative"
    return "mixed"


def build_rankings(
    summary: pd.DataFrame,
    by_month: pd.DataFrame,
    *,
    timeframe: str,
) -> pd.DataFrame:
    if summary.empty:
        return pd.DataFrame()
    # Aggregate both sides together per combo×horizon for ranking, plus per-side rows
    # User asked rankings for 5m and 15m — use both sides combined for expectancy ranking
    sub = summary[summary["timeframe"] == timeframe].copy()
    if sub.empty:
        return sub

    # Combine sides: recompute from cases is cleaner; approximate by weighting
    # Prefer grouping without side
    rows = []
    for (horizon, combo_id, target_pct, adverse_pct), g in sub.groupby(
        ["horizon", "combo_id", "target_pct", "adverse_pct"], dropna=False
    ):
        # Weighted pool across sides
        n = int(g["n_entries"].sum())
        n_tf = int(g["target_first_count"].sum())
        n_af = int(g["adverse_first_count"].sum())
        n_amb = int(g["ambiguous_count"].sum())
        n_nei = int(g["neither_count"].sum())
        n_decided = n_tf + n_af
        n_cons = n_tf + n_af + n_amb
        cons_wr = n_tf / n_cons if n_cons else None
        clean_wr = n_tf / n_decided if n_decided else None
        cons_exp = (
            cons_wr * float(target_pct) - (1.0 - cons_wr) * float(adverse_pct)
            if cons_wr is not None
            else None
        )
        # Monthly expectancy (both sides)
        month_exps: dict[str, float | None] = {}
        for m in MONTHS:
            mg = by_month[
                (by_month["timeframe"] == timeframe)
                & (by_month["horizon"] == horizon)
                & (by_month["combo_id"] == combo_id)
                & (by_month["fill_month"] == m)
            ]
            if mg.empty:
                month_exps[m] = None
                continue
            mt = int(mg["target_first_count"].sum())
            ma = int(mg["adverse_first_count"].sum())
            mm = int(mg["ambiguous_count"].sum())
            den = mt + ma + mm
            if den == 0:
                month_exps[m] = None
            else:
                wr = mt / den
                month_exps[m] = wr * float(target_pct) - (1.0 - wr) * float(adverse_pct)

        stab = _stability_flag(month_exps)
        months_positive = sum(1 for v in month_exps.values() if v is not None and v > 0)
        eligible = bool(
            cons_exp is not None
            and cons_exp > 0
            and n_decided >= MIN_DECIDED_FOR_RANK
            and months_positive >= 2
            and stab in {"stable_positive", "mixed"}
        )
        # mixed with positive overall and >=2 positive months OK if not sign_flip
        if stab == "unstable_sign_flip" or stab == "unstable_one_month_dominates":
            eligible = False
        if months_positive < 2:
            eligible = False

        rows.append(
            {
                "timeframe": timeframe,
                "horizon": horizon,
                "combo_id": combo_id,
                "target_pct": float(target_pct),
                "adverse_pct": float(adverse_pct),
                "n_entries": n,
                "n_decided": n_decided,
                "ambiguous_rate": n_amb / n if n else None,
                "conservative_win_rate": cons_wr,
                "clean_win_rate_decided_only": clean_wr,
                "conservative_expectancy_pct": cons_exp,
                "clean_expectancy_pct_decided_only": (
                    clean_wr * float(target_pct) - (1.0 - clean_wr) * float(adverse_pct)
                    if clean_wr is not None
                    else None
                ),
                "expectancy_2026_02": month_exps.get("2026-02"),
                "expectancy_2026_03": month_exps.get("2026-03"),
                "expectancy_2026_04": month_exps.get("2026-04"),
                "months_positive": months_positive,
                "stability": stab,
                "rank_eligible": eligible,
                "recommend_as_best": False,  # never auto-recommend
            }
        )
    rank = pd.DataFrame(rows)
    if rank.empty:
        return rank
    rank = rank.sort_values(
        by=["rank_eligible", "conservative_expectancy_pct", "n_decided", "ambiguous_rate"],
        ascending=[False, False, False, True],
    ).reset_index(drop=True)
    rank["rank"] = np.arange(1, len(rank) + 1)
    return rank


def write_report(
    output_dir: Path,
    *,
    meta: Mapping[str, Any],
    summary: pd.DataFrame,
    rank_5m: pd.DataFrame,
    rank_15m: pd.DataFrame,
) -> None:
    lines = [
        "# C3.5c First-Touch Audit (5m / 15m)",
        "",
        "Only real A6 TRIGGER→ENTRY fills. Annulled setups excluded. No majorDir.",
        "",
        f"- Symbol: `{meta.get('symbol')}` · Variant A6 · {meta.get('analyze_start')}→{meta.get('analyze_end')}",
        f"- Fills 5m: {meta.get('n_fills_5m')} · 15m: {meta.get('n_fills_15m')}",
        "",
        "## Simple readout",
        "",
    ]

    def _block(tf: str, rank: pd.DataFrame) -> None:
        lines.append(f"### {tf}")
        lines.append("")
        if summary.empty:
            lines.append("_No data._")
            lines.append("")
            return
        # Focus horizon 24h and 48h, both sides for top combos by target_first_rate
        for horizon in ("24h", "48h"):
            lines.append(f"**Horizon {horizon}** (both sides combined from ranking)")
            if rank.empty:
                lines.append("- no ranking rows")
                lines.append("")
                continue
            top = rank[rank["horizon"] == horizon].head(5)
            for _, r in top.iterrows():
                lines.append(
                    f"- `{r['combo_id']}`: cons_WR={None if pd.isna(r['conservative_win_rate']) else f'{100*r['conservative_win_rate']:.1f}%'} · "
                    f"cons_E={None if pd.isna(r['conservative_expectancy_pct']) else f'{r['conservative_expectancy_pct']:.3f}%'} · "
                    f"decided={int(r['n_decided'])} · amb={None if pd.isna(r['ambiguous_rate']) else f'{100*r['ambiguous_rate']:.1f}%'} · "
                    f"stab={r['stability']} · eligible={r['rank_eligible']}"
                )
            lines.append("")
        eligible = rank[rank["rank_eligible"] == True]  # noqa: E712
        lines.append(
            f"- Rank-eligible combos (positive cons E, ≥{MIN_DECIDED_FOR_RANK} decided, ≥2 positive months, not unstable): "
            f"**{len(eligible)}**"
        )
        lines.append("- No combo auto-recommended as best strategy.")
        lines.append("")

    _block("5m", rank_5m)
    _block("15m", rank_15m)
    lines.extend(
        [
            "## 5m vs 15m",
            "",
            "- 5m: more fills → more statistical mass, typically higher noise / ambiguous rate.",
            "- 15m: fewer fills → often cleaner but thinner samples; check decided counts carefully.",
            "- For further OOS: prefer timeframe with eligible ranks AND stable months; if none eligible, do not promote.",
            "",
        ]
    )
    (output_dir / "report.md").write_text("\n".join(lines), encoding="utf-8")


def run_first_touch_audit(
    *,
    symbol: str = "APTUSDT",
    output_dir: Path = DEFAULT_OUT,
    baseline_dir: Path = DEFAULT_BASELINE_DIR,
) -> dict[str, Any]:
    assert_safe_output_dir(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    baseline = assert_baseline_readonly(baseline_dir)
    if not baseline.get("hash_matches"):
        raise RuntimeError(
            f"baseline hash mismatch: expected {C2_BASELINE_HASH}, got {baseline.get('baseline_hash')}"
        )

    cfg = baseline_a6()
    all_cases: list[pd.DataFrame] = []
    fill_counts: dict[str, int] = {}

    for tf in TIMEFRAMES:
        frame = build_tf_frame(symbol, tf)
        _tl, entries, _lives = apply_pullback_entry(frame, cfg, return_lifecycles=True)
        parity_df, parity_rep = build_parity_table(
            frame, entries, variant=cfg.name, timeframe=tf, arming_type=cfg.arming_type
        )
        if not parity_rep["safe_to_compute_paths"]:
            raise RuntimeError(f"parity not safe for {tf}: {parity_rep}")
        fill_counts[tf] = int(parity_rep["n_python_fills"])
        cases = build_cases(frame, entries, timeframe=tf, variant=cfg.name)
        if not cases.empty:
            all_cases.append(cases)
        if not parity_df.empty:
            parity_df.to_csv(output_dir / f"parity_check_{tf}.csv", index=False)

    cases = pd.concat(all_cases, ignore_index=True) if all_cases else pd.DataFrame()
    summary = build_summary(cases)
    by_month = build_by_month(cases)
    amb = cases[cases["outcome"] == "ambiguous_same_bar"].copy() if not cases.empty else pd.DataFrame()
    rank_5m = build_rankings(summary, by_month, timeframe="5m")
    rank_15m = build_rankings(summary, by_month, timeframe="15m")

    cases.to_csv(output_dir / "first_touch_cases.csv", index=False)
    summary.to_csv(output_dir / "first_touch_summary.csv", index=False)
    by_month.to_csv(output_dir / "first_touch_by_month.csv", index=False)
    rank_5m.to_csv(output_dir / "first_touch_rank_5m.csv", index=False)
    rank_15m.to_csv(output_dir / "first_touch_rank_15m.csv", index=False)
    amb.to_csv(output_dir / "first_touch_ambiguous_cases.csv", index=False)

    meta = {
        "symbol": symbol,
        "variant": cfg.name,
        "config_hash": config_hash(cfg),
        "analyze_start": ANALYZE_START,
        "analyze_end": ANALYZE_END,
        "timeframes": list(TIMEFRAMES),
        "horizons": list(HORIZON_LABELS),
        "combos": [{"target_pct": a, "adverse_pct": b} for a, b in TARGET_ADVERSE_COMBOS],
        "n_fills_5m": fill_counts.get("5m"),
        "n_fills_15m": fill_counts.get("15m"),
        "min_decided_for_rank": MIN_DECIDED_FOR_RANK,
        "neither_excluded_from_expectancy_denom": True,
        "ambiguous_counts_as_loss_in_conservative": True,
        "baseline_reference_hash": C2_BASELINE_HASH,
        "production_sm_unchanged": True,
        "pine_unchanged": True,
        "no_recommendation_without_eligibility": True,
    }
    blob = json.dumps(json_safe({k: v for k, v in meta.items()}), sort_keys=True).encode()
    meta["content_hash"] = hashlib.sha1(blob).hexdigest()
    (output_dir / "metadata.json").write_text(json.dumps(json_safe(meta), indent=2), encoding="utf-8")
    write_report(output_dir, meta=meta, summary=summary, rank_5m=rank_5m, rank_15m=rank_15m)
    return meta


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="C3.5c first-touch audit")
    p.add_argument("--symbol", default="APTUSDT")
    p.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = p.parse_args(argv)
    meta = run_first_touch_audit(symbol=args.symbol, output_dir=args.out)
    print(json.dumps(json_safe(meta), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

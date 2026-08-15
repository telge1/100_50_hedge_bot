"""CLI: short_trend_pullback_v1 multicoin signal audit (research prototype).

Does not modify A6 / Pine / runtime. Max 12 frozen variants (B1–B3 × E1–E4).
"""

from __future__ import annotations

import argparse
import json
import time
import traceback
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from research.regime_scanner.candle_sources import load_regime_db_env_file
from research.regime_scanner.mysql_candle_store.config import load_regime_db_config
from research.regime_scanner.point_audit import json_safe
from research.regime_scanner.c35c_signal_store.path_store import C35cPathStore
from research.regime_scanner.short_trend_pullback.audit import (
    build_15m_frame,
    collect_variant_signals,
    load_a6_short_fills,
    metrics_block,
)
from research.regime_scanner.short_trend_pullback.config import (
    A6_PARENT_LABEL,
    CONTEXTS,
    DEFAULT_SYMBOLS,
    STRATEGY_VERSION,
    TRIGGERS,
    default_config,
    variant_id,
)
from research.regime_scanner.trend_weakening_multi_bar_audit import assert_safe_output_dir

DEFAULT_OUT = Path("research/regime_scanner/results/short_trend_pullback_v1_20260722")
DEFAULT_ENV = Path(
    "/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/"
    "research/regime_scanner/.env.regime_db"
)
TOP3 = {"BTCUSDT", "ETHUSDT", "SOLUSDT"}
MAJORS = {"BTCUSDT", "ETHUSDT", "BNBUSDT"}


def _write_docs(out_dir: Path, cfg_hash: str) -> None:
    (out_dir / "reuse_analysis.md").write_text(
        "\n".join(
            [
                "# Reuse analysis — short_trend_pullback_v1",
                "",
                "## Reused",
                "- `prepare_research_frame` / C3.4B edges (`protected_high`, BOS, CHOCH, micro)",
                "- `aggregate_complete_from_5m` + MySQL 5m loader (no feather fallback)",
                "- `path_arrays` / `first_touch_level` / `evaluate_outcome_on_fill`",
                "- `fixed_chrono_splits` / `assign_split` / warmup calendar 30d",
                "- A6 short fills from `multicoin_a6_signal_store_20260722` as frozen benchmark",
                "",
                "## Not reused as strategy core",
                "- A6 `step_pullback_entry` / arming-ready SM",
                "- Pine / runtime / bot",
                "",
                "## Newly built",
                "- B1/B2/B3 context predicates",
                "- Impulse → pullback → E1–E4 SM in `short_trend_pullback/`",
                f"- config hash `{cfg_hash}`",
                "",
            ]
        ),
        encoding="utf-8",
    )
    (out_dir / "strategy_semantics.md").write_text(
        "\n".join(
            [
                "# Strategy semantics — short_trend_pullback_v1",
                "",
                "| Merkmal | A6 | STP v1 |",
                "|---|---|---|",
                "| Side | Long+Short | Short only |",
                "| Kontext | multi-regime | bearish B1/B2/B3 before setup |",
                "| Pullback | A6 arming/ready | impulse → up-pullback → exhaustion |",
                "| Fill | next 15m open | next 15m open |",
                "| HTF/Regime | later diagnostic | part of setup birth |",
                "",
                "## B1",
                "EMA20 < EMA59 < EMA200; slopes_3(EMA20,EMA59) not rising; not persistently above EMA200.",
                "",
                "## B2",
                "major_direction==-1; protected_high present; no bullish external CHOCH.",
                "",
                "## B3",
                "B1 AND B2.",
                "",
                "## Impulse",
                "Starts on external bear BOS or major→bear; min 0.5 ATR net down over 2–32 bars; protected high intact.",
                "",
                "## Pullback",
                "Upward counter-move under protected high; invalidate on bull CHOCH / PH break / >16 bars / >0.786 retrace.",
                "",
                "## E1–E4",
                "E1 rejection; E2 micro flip; E3 EMA rejection; E4 pullback-low break. Trigger on closed 15m; fill next open.",
                "",
            ]
        ),
        encoding="utf-8",
    )


def evaluate_gates(row: dict[str, Any], a6: dict[str, Any]) -> dict[str, Any]:
    def _gt(a, b):
        return a is not None and b is not None and float(a) > float(b)

    def _ge(a, b):
        return a is not None and b is not None and float(a) >= float(b)

    checks = {
        "beats_a6_expectation": _gt(row.get("expectation"), a6.get("expectation")),
        "beats_a6_pf": _gt(row.get("pf"), a6.get("pf")),
        "beats_a6_equal_coin": _gt(row.get("equal_coin_expectation"), a6.get("equal_coin_expectation")),
        "beats_a6_median_coin": _gt(row.get("median_coin_expectation"), a6.get("median_coin_expectation")),
        "beats_a6_common_window": _gt(row.get("common_window_expectation"), a6.get("common_window_expectation")),
        "without_apt_ok": _ge(row.get("without_apt_expectation"), 0)
        or _gt(row.get("without_apt_expectation"), a6.get("without_apt_expectation")),
        "without_top3_ok": row.get("without_top3_expectation") is not None
        and float(row["without_top3_expectation"]) > -0.05,
        "pct_coins_positive_ge_60": (row.get("pct_coins_positive") or 0) >= 0.60,
        "validation_oos_not_opposed": True,  # filled below
        "enough_signals": int(row.get("n") or 0) >= 40,
        "no_coin_dominance": (row.get("max_coin_pnl_share") or 1) <= 0.60,
        "not_only_extreme_winners": (row.get("top_winner_share") or 1) <= 0.40,
        "mfe_mae_profile_ok": _gt(row.get("mean_mfe"), a6.get("mean_mfe"))
        or _gt(row.get("mean_mae"), a6.get("mean_mae")),  # less deep mae is greater (less negative)
        "causal": True,
    }
    vo = row.get("validation_expectation")
    oo = row.get("oos_expectation")
    # opposed if one strongly positive and other strongly negative
    if vo is not None and oo is not None:
        checks["validation_oos_not_opposed"] = not (
            (float(vo) > 0.05 and float(oo) < -0.05) or (float(oo) > 0.05 and float(vo) < -0.05)
        )
    checks["pass"] = all(checks.values())
    return checks


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--symbols", nargs="+", default=list(DEFAULT_SYMBOLS))
    p.add_argument("--data-source", default="mysql", choices=["mysql"])
    p.add_argument("--regime-db-env", type=Path, default=DEFAULT_ENV)
    p.add_argument("--contexts", nargs="+", default=list(CONTEXTS))
    p.add_argument("--triggers", nargs="+", default=list(TRIGGERS))
    p.add_argument("--strategy-version", default=STRATEGY_VERSION)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--persist", action="store_true")
    p.add_argument("--fail-if-existing", action="store_true")
    p.add_argument("--continue-on-symbol-error", action="store_true")
    p.add_argument("--output-dir", type=Path, default=DEFAULT_OUT)
    p.add_argument("--a6-parent-label", default=A6_PARENT_LABEL)
    args = p.parse_args(argv)

    assert_safe_output_dir(args.output_dir)
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    cfg = default_config()
    contexts = tuple(args.contexts)
    triggers = tuple(args.triggers)
    if len(contexts) * len(triggers) > 12:
        raise SystemExit("max 12 variants")

    load_regime_db_env_file(Path(args.regime_db_env))
    db = load_regime_db_config()
    path_store = C35cPathStore(db)

    _write_docs(out_dir, cfg.config_hash())
    variants = [{"context": c, "trigger": t, "variant": variant_id(c, t)} for c in contexts for t in triggers]
    pd.DataFrame(variants).to_csv(out_dir / "variant_definitions.csv", index=False)

    all_rows: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    frame_meta: dict[str, Any] = {}

    for sym in args.symbols:
        sym = sym.upper()
        t0 = time.time()
        try:
            frame, meta, a0, a1 = build_15m_frame(sym)
            frame_meta[sym] = meta
            rows = collect_variant_signals(
                sym, frame, a0, a1, contexts=contexts, triggers=triggers, cfg=cfg
            )
            all_rows.extend(rows)
            print(f"{sym}: {len(rows)} signal-rows in {time.time()-t0:.1f}s", flush=True)
        except Exception as exc:  # noqa: BLE001
            err = {"symbol": sym, "error": f"{type(exc).__name__}: {exc}", "traceback": traceback.format_exc()}
            errors.append(err)
            print(f"{sym}: ERROR {err['error']}", flush=True)
            if not args.continue_on_symbol_error:
                raise

    panel = pd.DataFrame(all_rows)
    if panel.empty:
        (out_dir / "metadata.json").write_text(
            json.dumps({"error": "no_signals", "errors": errors}, indent=2), encoding="utf-8"
        )
        path_store.close()
        return 1

    panel["fill_timestamp"] = pd.to_datetime(panel["fill_timestamp"], utc=True)
    panel.to_csv(out_dir / "signals_per_trade.csv", index=False)
    feat_cols = [c for c in panel.columns if c.startswith("feat_") or c in (
        "symbol", "variant", "context", "trigger", "fill_timestamp", "entry_price",
        "pullback_retracement", "impulse_strength", "protected_high",
    )]
    panel[feat_cols].to_csv(out_dir / "signal_features.csv", index=False)

    # counts
    counts = (
        panel.groupby(["variant", "context", "trigger", "symbol"], as_index=False)
        .size()
        .rename(columns={"size": "n_signals"})
    )
    counts.to_csv(out_dir / "signal_counts_by_variant.csv", index=False)

    # forward outcomes / first touch / mfe
    fwd_cols = [c for c in panel.columns if c.startswith("mfe_") or c.startswith("mae_") or c.startswith("dir_close") or c.startswith("ft_")]
    panel[["symbol", "variant", "fill_timestamp", "net_pnl_pct", "exit_reason"] + fwd_cols].to_csv(
        out_dir / "forward_outcomes.csv", index=False
    )
    ft_rows = []
    for v, g in panel.groupby("variant"):
        row = {"variant": v, "n": len(g)}
        for c in g.columns:
            if c.endswith("_reached"):
                row[c] = float(pd.to_numeric(g[c], errors="coerce").mean())
        ft_rows.append(row)
    pd.DataFrame(ft_rows).to_csv(out_dir / "first_touch_summary.csv", index=False)

    mfe_rows = []
    for v, g in panel.groupby("variant"):
        for h in (4, 8, 16, 24, 48, 96, 192):
            mfe_rows.append(
                {
                    "variant": v,
                    "horizon": h,
                    "mean_mfe": float(pd.to_numeric(g.get(f"mfe_pct_h{h}"), errors="coerce").mean()),
                    "mean_mae": float(pd.to_numeric(g.get(f"mae_pct_h{h}"), errors="coerce").mean()),
                    "mean_dir_close": float(pd.to_numeric(g.get(f"dir_close_h{h}"), errors="coerce").mean()),
                }
            )
    pd.DataFrame(mfe_rows).to_csv(out_dir / "mfe_mae_by_horizon.csv", index=False)

    # A6 short benchmark
    a6 = load_a6_short_fills(path_store, args.a6_parent_label)
    a6_m = metrics_block(a6["net_pnl_pct"].to_numpy(dtype=float)) if len(a6) else metrics_block([])
    a6_by_coin = {}
    if len(a6):
        for sym, g in a6.groupby(a6.symbol.astype(str)):
            a6_by_coin[sym] = float(pd.to_numeric(g.net_pnl_pct, errors="coerce").mean())
    a6_equal = float(np.mean(list(a6_by_coin.values()))) if a6_by_coin else None
    a6_median = float(np.median(list(a6_by_coin.values()))) if a6_by_coin else None
    a6_wo_apt = metrics_block(a6[a6.symbol != "APTUSDT"]["net_pnl_pct"].to_numpy(dtype=float)) if len(a6) else a6_m
    # common window for A6: days with >= half symbols
    a6_cw_exp = None
    if len(a6):
        a6 = a6.copy()
        a6["day"] = a6["fill_time"].dt.floor("D")
        nsym = a6["symbol"].nunique()
        days = a6.groupby("day")["symbol"].nunique()
        keep = set(days[days >= max(2, nsym // 2)].index)
        a6_cw = a6[a6["day"].isin(keep)]
        a6_cw_exp = metrics_block(a6_cw["net_pnl_pct"].to_numpy(dtype=float))["expectation"]

    a6_ref = {
        "expectation": a6_m["expectation"],
        "pf": a6_m["pf"],
        "equal_coin_expectation": a6_equal,
        "median_coin_expectation": a6_median,
        "common_window_expectation": a6_cw_exp,
        "without_apt_expectation": a6_wo_apt["expectation"],
        "mean_mfe": float(pd.to_numeric(a6.get("mfe_pct"), errors="coerce").mean()) if "mfe_pct" in a6.columns else None,
        "mean_mae": float(pd.to_numeric(a6.get("mae_pct"), errors="coerce").mean()) if "mae_pct" in a6.columns else None,
        "n": a6_m["n"],
    }

    # Overlap: match symbol + fill_timestamp within 1 minute
    overlap_rows = []
    if len(a6):
        a6_keys = set(zip(a6.symbol.astype(str), a6.fill_time.dt.floor("min")))
        for v, g in panel.groupby("variant"):
            keys = set(zip(g.symbol.astype(str), g.fill_timestamp.dt.floor("min")))
            inter = keys & a6_keys
            overlap_rows.append(
                {
                    "variant": v,
                    "n_stp": len(keys),
                    "n_a6_short": len(a6_keys),
                    "n_overlap": len(inter),
                    "overlap_rate_vs_stp": float(len(inter) / len(keys)) if keys else None,
                    "stp_only": len(keys - a6_keys),
                    "a6_only_global": len(a6_keys - keys),
                }
            )
    pd.DataFrame(overlap_rows).to_csv(out_dir / "signal_overlap_with_a6.csv", index=False)

    # per-variant summaries
    def slice_metrics(df: pd.DataFrame) -> dict[str, Any]:
        m = metrics_block(pd.to_numeric(df["net_pnl_pct"], errors="coerce").to_numpy(dtype=float))
        m["mean_mfe"] = float(pd.to_numeric(df.get("mfe_pct"), errors="coerce").mean())
        m["mean_mae"] = float(pd.to_numeric(df.get("mae_pct"), errors="coerce").mean())
        m["median_hold"] = float(pd.to_numeric(df.get("bars_held"), errors="coerce").median())
        reasons = df["exit_reason"].value_counts(normalize=True).to_dict() if len(df) else {}
        m["tp_share"] = float(reasons.get("TP", 0.0))
        m["sl_share"] = float(reasons.get("SL", 0.0) + reasons.get("same_bar_conservative_sl", 0.0))
        m["time_exit_share"] = float(reasons.get("time_exit", 0.0))
        return m

    global_rows = []
    by_coin_rows = []
    equal_rows = []
    split_rows = []
    cw_rows = []
    wo_apt_rows = []
    wo_top1_rows = []
    wo_top3_rows = []
    majors_rows = []
    gate_rows = []
    bench_rows = []
    cmp_rows = []

    # common window days across panel
    panel = panel.copy()
    panel["day"] = panel["fill_timestamp"].dt.floor("D")
    nsym = panel["symbol"].nunique()
    day_counts = panel.groupby("day")["symbol"].nunique()
    common_days = set(day_counts[day_counts >= max(2, nsym // 2)].index)

    for v, g in panel.groupby("variant"):
        gm = slice_metrics(g)
        coin_exp = {}
        for sym, cg in g.groupby(g.symbol.astype(str)):
            cm = slice_metrics(cg)
            coin_exp[sym] = cm["expectation"]
            by_coin_rows.append({"variant": v, "symbol": sym, **cm})
        pos_share = float(np.mean([e is not None and e > 0 for e in coin_exp.values()])) if coin_exp else None
        equal = float(np.mean([e for e in coin_exp.values() if e is not None])) if coin_exp else None
        median_c = float(np.median([e for e in coin_exp.values() if e is not None])) if coin_exp else None
        # dominance
        coin_sums = g.groupby(g.symbol.astype(str))["net_pnl_pct"].sum()
        pos_sums = coin_sums[coin_sums > 0]
        max_share = float(pos_sums.max() / pos_sums.sum()) if len(pos_sums) and pos_sums.sum() > 0 else 0.0
        wins = g[pd.to_numeric(g.net_pnl_pct, errors="coerce") > 0]["net_pnl_pct"]
        top_w_share = float(wins.nlargest(max(1, len(wins) // 10)).sum() / wins.sum()) if len(wins) and wins.sum() > 0 else 0.0

        equal_rows.append(
            {"variant": v, "equal_coin_expectation": equal, "median_coin_expectation": median_c, "pct_coins_positive": pos_share, "n_coins": len(coin_exp)}
        )

        for sp, sg in g.groupby(g.split.astype(str)):
            split_rows.append({"variant": v, "split": sp, **slice_metrics(sg)})

        cw = g[g["day"].isin(common_days)]
        cwm = slice_metrics(cw)
        cw_rows.append({"variant": v, **cwm})

        wo_apt = g[g.symbol != "APTUSDT"]
        wo_apt_m = slice_metrics(wo_apt)
        wo_apt_rows.append({"variant": v, **wo_apt_m})

        # without top1 by expectation
        if coin_exp:
            top1 = max(coin_exp.items(), key=lambda kv: (kv[1] is not None, kv[1] or -999))[0]
            wo1 = g[g.symbol != top1]
            wo_top1_rows.append({"variant": v, "excluded": top1, **slice_metrics(wo1)})
        wo3 = g[~g.symbol.isin(TOP3)]
        wo3m = slice_metrics(wo3)
        wo_top3_rows.append({"variant": v, **wo3m})

        maj = g[g.symbol.isin(MAJORS)]
        alt = g[~g.symbol.isin(MAJORS)]
        majors_rows.append({"variant": v, "bucket": "majors_btc_eth_bnb", **slice_metrics(maj)})
        majors_rows.append({"variant": v, "bucket": "altcoins", **slice_metrics(alt)})

        # splits expectations
        val_e = next((r["expectation"] for r in split_rows if r["variant"] == v and r["split"] == "validation"), None)
        # fix: just computed in loop — recompute
        val_e = slice_metrics(g[g.split == "validation"])["expectation"]
        oos_e = slice_metrics(g[g.split == "oos"])["expectation"]
        dev_e = slice_metrics(g[g.split == "dev"])["expectation"]

        row = {
            "variant": v,
            **gm,
            "equal_coin_expectation": equal,
            "median_coin_expectation": median_c,
            "pct_coins_positive": pos_share,
            "common_window_expectation": cwm["expectation"],
            "without_apt_expectation": wo_apt_m["expectation"],
            "without_top3_expectation": wo3m["expectation"],
            "validation_expectation": val_e,
            "oos_expectation": oos_e,
            "dev_expectation": dev_e,
            "max_coin_pnl_share": max_share,
            "top_winner_share": top_w_share,
            "signals_per_1000_bars": None,
        }
        # density approx
        n_bars_est = sum(int(frame_meta[s].get("n_15m") or 0) for s in frame_meta)
        if n_bars_est:
            row["signals_per_1000_bars"] = float(len(g) / n_bars_est * 1000.0)
        global_rows.append(row)

        gates = evaluate_gates(row, a6_ref)
        gate_rows.append({"variant": v, **gates})

        ov = next((o for o in overlap_rows if o["variant"] == v), {})
        bench_rows.append(
            {
                "variant": v,
                "stp_n": gm["n"],
                "stp_expectation": gm["expectation"],
                "stp_pf": gm["pf"],
                "a6_n": a6_ref["n"],
                "a6_expectation": a6_ref["expectation"],
                "a6_pf": a6_ref["pf"],
                "overlap_rate": ov.get("overlap_rate_vs_stp"),
                "stp_only": ov.get("stp_only"),
            }
        )
        cmp_rows.append(
            {
                "variant": v,
                "delta_expectation": None
                if gm["expectation"] is None or a6_ref["expectation"] is None
                else float(gm["expectation"]) - float(a6_ref["expectation"]),
                "delta_pf": None
                if gm["pf"] is None or a6_ref["pf"] is None
                else float(gm["pf"]) - float(a6_ref["pf"]),
                "overlap_rate": ov.get("overlap_rate_vs_stp"),
            }
        )

    pd.DataFrame(global_rows).to_csv(out_dir / "variant_global_summary.csv", index=False)
    pd.DataFrame(by_coin_rows).to_csv(out_dir / "variant_by_coin.csv", index=False)
    pd.DataFrame(equal_rows).to_csv(out_dir / "variant_equal_coin.csv", index=False)
    pd.DataFrame(split_rows).to_csv(out_dir / "variant_by_split.csv", index=False)
    pd.DataFrame(cw_rows).to_csv(out_dir / "variant_common_window.csv", index=False)
    pd.DataFrame(wo_apt_rows).to_csv(out_dir / "variant_without_apt.csv", index=False)
    pd.DataFrame(wo_top1_rows).to_csv(out_dir / "variant_without_top1.csv", index=False)
    pd.DataFrame(wo_top3_rows).to_csv(out_dir / "variant_without_top3.csv", index=False)
    pd.DataFrame(majors_rows).to_csv(out_dir / "variant_majors_vs_altcoins.csv", index=False)
    pd.DataFrame(bench_rows).to_csv(out_dir / "tp3_sl2_benchmark.csv", index=False)
    pd.DataFrame(cmp_rows).to_csv(out_dir / "a6_short_comparison.csv", index=False)
    pd.DataFrame(gate_rows).to_csv(out_dir / "candidate_gate_results.csv", index=False)

    # abort if near-identical to A6
    max_overlap = max((o.get("overlap_rate_vs_stp") or 0) for o in overlap_rows) if overlap_rows else 0
    semantic_fail = max_overlap >= 0.85

    passed = [g for g in gate_rows if g.get("pass")]
    best = None
    if passed and not semantic_fail:
        # pick best by expectation among passers
        pass_vars = {g["variant"] for g in passed}
        cands = [r for r in global_rows if r["variant"] in pass_vars]
        best = max(cands, key=lambda r: (r.get("expectation") is not None, r.get("expectation") or -999))
    track_rejected = best is None

    # best context / trigger by mean expectation
    gdf = pd.DataFrame(global_rows)
    gdf["context"] = gdf["variant"].str.extract(r"__(B[123])__")[0]
    gdf["trigger"] = gdf["variant"].str.extract(r"__(E[1234])$")[0]
    best_ctx = gdf.groupby("context")["expectation"].mean().sort_values(ascending=False)
    best_trig = gdf.groupby("trigger")["expectation"].mean().sort_values(ascending=False)

    report = []
    report.append("# short_trend_pullback_v1 — Abschlussbericht\n\n")
    report.append("Research prototype only. No A6/Pine/runtime change. No exit optimization.\n\n")
    report.append(f"- Config hash: `{cfg.config_hash()}`\n")
    report.append(f"- Variants: {len(variants)}\n")
    report.append(f"- Total signal rows: {len(panel)}\n")
    report.append(f"- Max overlap vs A6-short: {max_overlap:.3f}\n")
    report.append(f"- Semantic fail (overlap≥0.85): {semantic_fail}\n")
    report.append(f"- Track rejected: {track_rejected}\n")
    if best:
        report.append(f"- Recommended for phase-2 falsification only: `{best['variant']}`\n")
    else:
        report.append("- No variant recommended.\n")
    report.append("\n## Best context / trigger (by mean E)\n")
    report.append(f"- Context: {best_ctx.index[0] if len(best_ctx) else None} ({best_ctx.iloc[0] if len(best_ctx) else None})\n")
    report.append(f"- Trigger: {best_trig.index[0] if len(best_trig) else None} ({best_trig.iloc[0] if len(best_trig) else None})\n")
    report.append("\n## Global expectations\n")
    for _, r in gdf.sort_values("expectation", ascending=False).iterrows():
        report.append(
            f"- {r['variant']}: n={int(r['n'])} E={r['expectation']} PF={r['pf']} "
            f"equal={r['equal_coin_expectation']} coins+={r['pct_coins_positive']}\n"
        )
    report.append("\n## A6-short reference\n")
    report.append(f"- n={a6_ref['n']} E={a6_ref['expectation']} PF={a6_ref['pf']}\n")
    (out_dir / "short_trend_pullback_report.md").write_text("".join(report), encoding="utf-8")

    meta = {
        "strategy_version": args.strategy_version,
        "config": cfg.to_dict(),
        "config_hash": cfg.config_hash(),
        "contexts": list(contexts),
        "triggers": list(triggers),
        "n_variants": len(variants),
        "n_signal_rows": int(len(panel)),
        "n_symbols": int(panel.symbol.nunique()),
        "errors": errors,
        "a6_reference": a6_ref,
        "max_overlap_vs_a6_short": max_overlap,
        "semantic_fail_high_overlap": semantic_fail,
        "track_rejected": track_rejected,
        "recommended_variant": None if best is None else best.get("variant"),
        "best_context": None if best_ctx.empty else str(best_ctx.index[0]),
        "best_trigger": None if best_trig.empty else str(best_trig.index[0]),
        "auto_activate": False,
        "exit_optimization": False,
        "a6_changed": False,
        "pine_changed": False,
        "runtime_changed": False,
        "commit": False,
        "push": False,
        "persist_requested": bool(args.persist),
        "dry_run": bool(args.dry_run or not args.persist),
        "note": "Signal MySQL persist deferred unless --persist and gates warrant; CSV audit is primary deliverable.",
    }
    (out_dir / "metadata.json").write_text(json.dumps(json_safe(meta), indent=2), encoding="utf-8")
    path_store.close()
    print(json.dumps(json_safe(meta), indent=2))
    return 0 if not errors else (0 if args.continue_on_symbol_error else 1)


if __name__ == "__main__":
    raise SystemExit(main())

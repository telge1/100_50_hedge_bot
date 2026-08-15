"""CLI: liquidity_sweep_reclaim_v1 multicoin dry-run audit.

Does not modify A6 / STP / Pine / runtime. Max 18 frozen variants (L1–L2 × P × R).
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
from research.regime_scanner.c35c_signal_store.path_store import C35cPathStore
from research.regime_scanner.liquidity_sweep_reclaim.audit import (
    apply_candidate_gates,
    build_15m_frame,
    collect_symbol_signals,
    load_a6_fills,
    load_stp_b2e1_fills,
    metrics_block,
    overlap_table,
    pick_candidates,
    slice_summaries,
)
from research.regime_scanner.liquidity_sweep_reclaim.config import (
    A6_PARENT_LABEL,
    DEFAULT_SYMBOLS,
    EXIT_BENCHMARKS,
    LEVEL_FAMILIES,
    LEVEL_FAMILIES_REQUESTED,
    MFE_HORIZONS,
    PENETRATIONS,
    RECLAIMS,
    STRATEGY_VERSION,
    all_variants,
    default_config,
    variant_id,
)
from research.regime_scanner.liquidity_sweep_reclaim.levels import (
    L3_AVAILABLE,
    L3_UNAVAILABLE_REASON,
)
from research.regime_scanner.liquidity_sweep_reclaim.sequential import apply_sequential
from research.regime_scanner.liquidity_sweep_reclaim.store import LSRStore
from research.regime_scanner.mysql_candle_store.config import load_regime_db_config
from research.regime_scanner.point_audit import json_safe
from research.regime_scanner.trend_weakening_multi_bar_audit import assert_safe_output_dir

DEFAULT_OUT = Path("research/regime_scanner/results/liquidity_sweep_reclaim_v1_20260722")
DEFAULT_ENV = Path(
    "/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/"
    "research/regime_scanner/.env.regime_db"
)


def _write_static_docs(out_dir: Path, cfg_hash: str) -> None:
    # reuse_analysis / implementation_plan already seeded; refresh strategy_semantics
    (out_dir / "strategy_semantics.md").write_text(
        "\n".join(
            [
                "# Strategy semantics — liquidity_sweep_reclaim_v1",
                "",
                "## Level families",
                "- L1: C3.1 confirmed range_high / range_low (prior bar only)",
                "- L2: C3.4B protected_high / protected_low (prior bar only)",
                f"- L3: UNAVAILABLE — {L3_UNAVAILABLE_REASON}",
                "",
                "## Sweep",
                "- Long: low < level; Short: high > level",
                "- P1 any+; P2 ≥0.10 ATR; P3 ≥0.25 ATR; max 1.00 ATR else oversized_break",
                "",
                "## Reclaim",
                "- R1 same-candle close reclaim → trigger → next open fill",
                "- R2 reclaim on exactly next bar",
                "- R3 reclaim (same or next) + one confirmation bar",
                "",
                "## State machine",
                "LEVEL_ELIGIBLE → SWEPT → RECLAIMED → [CONFIRMED] → TRIGGERED → FILLED / INVALIDATED",
                "",
                f"## Config hash `{cfg_hash}`",
                "",
            ]
        ),
        encoding="utf-8",
    )
    variants = all_variants()
    pd.DataFrame(
        [
            {
                "variant": v,
                "level_family": v.split("x")[0],
                "penetration": v.split("x")[1],
                "reclaim": v.split("x")[2],
            }
            for v in variants
        ]
    ).to_csv(out_dir / "variant_definitions.csv", index=False)


def _signal_density(signals: pd.DataFrame, meta_by_sym: dict[str, dict]) -> pd.DataFrame:
    rows = []
    if signals.empty:
        return pd.DataFrame()
    for (variant, side, symbol), g in signals.groupby(["variant", "side", "symbol"]):
        n15 = int(meta_by_sym.get(symbol, {}).get("n_15m") or 0)
        dens = (len(g) / n15 * 1000.0) if n15 else None
        rows.append(
            {
                "variant": variant,
                "side": side,
                "symbol": symbol,
                "n": len(g),
                "n_15m": n15,
                "density_per_1000": dens,
            }
        )
    return pd.DataFrame(rows)


def build_report(out_dir: Path, meta: dict[str, Any]) -> None:
    gates = pd.read_csv(out_dir / "candidate_gate_results.csv") if (out_dir / "candidate_gate_results.csv").exists() else pd.DataFrame()
    cand = meta.get("candidates") or {}
    seq = pd.read_csv(out_dir / "sequential_summary.csv") if (out_dir / "sequential_summary.csv").exists() else pd.DataFrame()
    ind = pd.read_csv(out_dir / "independent_summary.csv") if (out_dir / "independent_summary.csv").exists() else pd.DataFrame()
    signals = pd.read_csv(out_dir / "signals.csv") if (out_dir / "signals.csv").exists() else pd.DataFrame()
    setups = pd.read_csv(out_dir / "setup_events.csv") if (out_dir / "setup_events.csv").exists() else pd.DataFrame()

    def _best_row(df, variant, side, exit_id="X5"):
        if df.empty:
            return None
        m = df[(df["variant"] == variant) & (df["side"] == side) & (df["exit_id"] == exit_id)]
        return m.iloc[0].to_dict() if len(m) else None

    lines = [
        "# liquidity_sweep_reclaim_v1 — Abschlussbericht",
        "",
        f"1. Wiederverwendet: C3.1 range replay, C3.4B protected levels, MySQL 5m→15m, "
        f"path_arrays/first_touch/evaluate_outcome_params, splits, A6/STP benchmarks.",
        f"2. Level-Familien: L1+L2 verfügbar; L3 nicht verfügbar.",
        "3. L1: C3.1 `in_range` + prior-bar `range_high`/`range_low`, min age 3.",
        "4. L2: prior-bar `protected_low` (long) / `protected_high` (short).",
        f"5. L3: {L3_UNAVAILABLE_REASON}",
        "6. Sweep: long low<level / short high>level; ≤1.00 ATR.",
        "7. P1 any+; P2≥0.10 ATR; P3≥0.25 ATR.",
        "8. R1 same-candle; R2 next-bar; R3 reclaim+confirmation.",
        "9. SM: ELIGIBLE→SWEPT→RECLAIMED→[CONFIRMED]→TRIGGERED→FILLED / INVALIDATED.",
        "10. Invalidierungen: oversized, window miss, deeper break, level replace, external BOS/CHOCH, fill missing, data gap/warmup.",
        "11. Kausalität: Level vor Sweep (prior bar); Trigger am Close; Fill am nächsten Open.",
        f"12. Varianten: {meta.get('n_variants')} (L3 excluded).",
        f"13. Setups (events): {meta.get('n_setups')}",
        f"14. Signale: {meta.get('n_signals')}",
        f"15. Long/Short: {meta.get('n_long')} / {meta.get('n_short')}",
        f"16. Je Coin: siehe signal_counts_by_coin.csv",
        f"17. Signaldichte: siehe signal_counts_by_variant.csv / density",
        "18. Forward-MFE/MAE: siehe mfe_mae_by_horizon.csv / forward_outcomes.csv",
        "19. First-Touch: siehe first_touch_summary.csv",
    ]
    for xid in ("X1", "X2", "X3", "X4", "X5"):
        lines.append(f"{20 + ['X1','X2','X3','X4','X5'].index(xid)}. {xid}: siehe independent/sequential_summary exit_id={xid}")
    lines += [
        "25. Independent: independent_summary.csv",
        "26. Sequential: sequential_summary.csv",
        f"27. Beste Long-Variante: {cand.get('best_long')}",
        f"28. Beste Short-Variante: {cand.get('best_short')}",
        f"29. Bester Gesamtkandidat: {cand.get('best_overall')}",
        "30–32. Dev/Validation/OOS: summary_by_split.csv",
        "33–35. Equal/Median/Common: summary_equal_coin / median / common_window",
        "36–38. ohne APT/Top1/Top3: entsprechende CSVs",
        "39–40. Majors/Altcoins: summary_majors_vs_altcoins.csv",
        "41. Positive Coins: summary_equal_coin pct_positive_coins",
        "42. Same-Bar-Rate: sequential_summary same_bar_rate",
        "43. Kosten 0.25%: exit_id *_c025 in summaries",
        "44. Overlap A6/STP: overlap_with_a6_stp.csv",
        f"45. Candidate-Gates: all_pass count={cand.get('n_passed')}",
        f"46. Max eine Variante je Side Phase2: long={cand.get('best_long')} short={cand.get('best_short')}",
        f"47. Track-Verdict: **{cand.get('track_verdict')}**",
        "48. Keine Schwellen-Nachoptimierung.",
        "49. Keine automatische Aktivierung.",
        "50. Keine A6-/STP-/Pine-Änderung.",
        "51. Keine Runtime-/Bot-Änderung.",
        f"52. Persist: {meta.get('persist_status')}",
        "53. Kein Commit.",
        "54. Kein Push.",
        "",
    ]
    (out_dir / "liquidity_sweep_reclaim_report.md").write_text("\n".join(lines), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--symbols", nargs="+", default=list(DEFAULT_SYMBOLS))
    p.add_argument("--data-source", default="mysql", choices=["mysql"])
    p.add_argument("--regime-db-env", type=Path, default=DEFAULT_ENV)
    p.add_argument("--level-families", nargs="+", default=list(LEVEL_FAMILIES_REQUESTED))
    p.add_argument("--penetrations", nargs="+", default=list(PENETRATIONS))
    p.add_argument("--reclaims", nargs="+", default=list(RECLAIMS))
    p.add_argument("--strategy-version", default=STRATEGY_VERSION)
    p.add_argument("--independent", action="store_true", default=True)
    p.add_argument("--sequential", action="store_true", default=True)
    p.add_argument("--dry-run", action="store_true", default=True)
    p.add_argument("--persist", action="store_true")
    p.add_argument("--output-dir", type=Path, default=DEFAULT_OUT)
    p.add_argument("--continue-on-symbol-error", action="store_true")
    p.add_argument("--a6-parent-label", default=A6_PARENT_LABEL)
    args = p.parse_args(argv)

    out_dir = args.output_dir
    assert_safe_output_dir(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    load_regime_db_env_file(Path(args.regime_db_env))
    db = load_regime_db_config()

    cfg = default_config()
    cfg_hash = cfg.config_hash()
    _write_static_docs(out_dir, cfg_hash)

    level_families = tuple(args.level_families)
    penetrations = tuple(args.penetrations)
    reclaims = tuple(args.reclaims)
    variants = all_variants(
        tuple(lf for lf in level_families if lf != "L3" or L3_AVAILABLE),
        penetrations,
        reclaims,
    )

    all_signals: list[dict[str, Any]] = []
    all_trades: list[dict[str, Any]] = []
    all_setups: list[dict[str, Any]] = []
    meta_by_sym: dict[str, dict] = {}
    errors: list[dict[str, str]] = []
    t0 = time.time()

    for sym in args.symbols:
        print(f"[LSR] building frame {sym} ...", flush=True)
        try:
            frame, meta, a0, a1 = build_15m_frame(sym)
            meta_by_sym[sym] = meta
            print(f"[LSR] {sym}: n15m={len(frame)} analyze={a0}→{a1}", flush=True)
            sigs, trades, setups = collect_symbol_signals(
                sym,
                frame,
                a0,
                a1,
                level_families=level_families,
                penetrations=penetrations,
                reclaims=reclaims,
                cfg=cfg,
            )
            all_signals.extend(sigs)
            all_trades.extend(trades)
            all_setups.extend(setups)
            print(f"[LSR] {sym}: signals={len(sigs)} setups={len(setups)}", flush=True)
        except Exception as exc:  # noqa: BLE001
            errors.append({"symbol": sym, "error": f"{exc}\n{traceback.format_exc()}"})
            print(f"[LSR] ERROR {sym}: {exc}", flush=True)
            if not args.continue_on_symbol_error:
                raise

    signals = pd.DataFrame(all_signals)
    trades = pd.DataFrame(all_trades)
    setups = pd.DataFrame(all_setups)

    # level inventory
    inv_rows = []
    for sym, meta in meta_by_sym.items():
        inv_rows.append(
            {
                "symbol": sym,
                "l1_available": True,
                "l2_available": True,
                "l3_available": L3_AVAILABLE,
                "n_15m": meta.get("n_15m"),
                "analyze_start": meta.get("analyze_start"),
                "analyze_end": meta.get("analyze_end_exclusive"),
            }
        )
    pd.DataFrame(inv_rows).to_csv(out_dir / "level_inventory.csv", index=False)

    signals.to_csv(out_dir / "signals.csv", index=False)
    if not signals.empty:
        feat_cols = [c for c in signals.columns if c.startswith("feat_")]
        base_cols = [
            "symbol",
            "variant",
            "side",
            "setup_id",
            "fill_timestamp",
            "level_family",
            "penetration_class",
            "reclaim_type",
        ]
        signals[base_cols + feat_cols].to_csv(out_dir / "signal_features.csv", index=False)
        fwd_cols = [c for c in signals.columns if c.startswith("h") or c.startswith("ft_") or c in {
            "same_bar_ambiguous", "first_touch_order", "favorable_first", "adverse_first"
        }]
        signals[base_cols + fwd_cols].to_csv(out_dir / "forward_outcomes.csv", index=False)

        # mfe/mae by horizon
        mh = []
        for h in MFE_HORIZONS:
            col_mfe, col_mae = f"h{h}_mfe_pct", f"h{h}_mae_pct"
            if col_mfe in signals.columns:
                mh.append(
                    {
                        "horizon": h,
                        "mean_mfe": float(signals[col_mfe].mean()),
                        "mean_mae": float(signals[col_mae].mean()),
                        "median_mfe": float(signals[col_mfe].median()),
                        "median_mae": float(signals[col_mae].median()),
                    }
                )
        pd.DataFrame(mh).to_csv(out_dir / "mfe_mae_by_horizon.csv", index=False)

        ft_rows = []
        for c in signals.columns:
            if c.startswith("ft_") and c.endswith("_reached"):
                ft_rows.append({"level": c, "rate": float(signals[c].mean())})
        pd.DataFrame(ft_rows).to_csv(out_dir / "first_touch_summary.csv", index=False)

        signals.groupby(["variant"]).size().reset_index(name="n").to_csv(
            out_dir / "signal_counts_by_variant.csv", index=False
        )
        signals.groupby(["symbol"]).size().reset_index(name="n").to_csv(
            out_dir / "signal_counts_by_coin.csv", index=False
        )
        signals.groupby(["side"]).size().reset_index(name="n").to_csv(
            out_dir / "signal_counts_by_side.csv", index=False
        )
        _signal_density(signals, meta_by_sym).to_csv(out_dir / "signal_density.csv", index=False)
    else:
        for name in (
            "signal_features.csv",
            "forward_outcomes.csv",
            "mfe_mae_by_horizon.csv",
            "first_touch_summary.csv",
            "signal_counts_by_variant.csv",
            "signal_counts_by_coin.csv",
            "signal_counts_by_side.csv",
        ):
            pd.DataFrame().to_csv(out_dir / name, index=False)

    setups.to_csv(out_dir / "setup_events.csv", index=False)
    trades.to_csv(out_dir / "exit_benchmark_trade_level.csv", index=False)

    summaries = slice_summaries(trades, signals) if not trades.empty else {}
    for name, df in summaries.items():
        if isinstance(df, pd.DataFrame):
            df.to_csv(out_dir / f"{name}.csv", index=False)

    # ensure expected summary files exist
    for name in (
        "independent_summary.csv",
        "sequential_summary.csv",
        "summary_by_coin.csv",
        "summary_equal_coin.csv",
        "summary_median_coin.csv",
        "summary_by_split.csv",
        "summary_common_window.csv",
        "summary_without_apt.csv",
        "summary_without_top1.csv",
        "summary_without_top3.csv",
        "summary_majors_vs_altcoins.csv",
        "summary_by_month.csv",
        "summary_by_level_family.csv",
        "summary_by_penetration.csv",
        "summary_by_reclaim_type.csv",
    ):
        if not (out_dir / name).exists():
            pd.DataFrame().to_csv(out_dir / name, index=False)

    # benchmarks + overlap
    try:
        store = C35cPathStore(db)
        a6 = load_a6_fills(store, args.a6_parent_label)
    except Exception as exc:  # noqa: BLE001
        print(f"[LSR] A6 load failed: {exc}", flush=True)
        a6 = pd.DataFrame()
    try:
        stp = load_stp_b2e1_fills()
    except Exception as exc:  # noqa: BLE001
        print(f"[LSR] STP load failed: {exc}", flush=True)
        stp = pd.DataFrame()
    ov_frames = []
    if not signals.empty:
        if not a6.empty:
            ov_frames.append(overlap_table(signals, a6, "A6"))
            a6_long = a6[a6["side"] == "long"]
            a6_short = a6[a6["side"] == "short"]
            if len(a6_long):
                ov_frames.append(overlap_table(signals, a6_long, "A6_long"))
            if len(a6_short):
                ov_frames.append(overlap_table(signals, a6_short, "A6_short"))
        if not stp.empty:
            ov_frames.append(overlap_table(signals, stp, "STP_B2xE1"))
    overlap = pd.concat(ov_frames, ignore_index=True) if ov_frames else pd.DataFrame()
    overlap.to_csv(out_dir / "overlap_with_a6_stp.csv", index=False)

    # benchmark comparison on X5
    bench_rows = []
    if not trades.empty:
        seq_t = apply_sequential(trades[trades["exit_id"] == "X5"].copy())
        taken = seq_t[seq_t["taken_sequential"] == True]  # noqa: E712
        for (variant, side), g in taken.groupby(["variant", "side"]):
            m = metrics_block(g["net_pnl_pct"].to_numpy())
            bench_rows.append({"source": "LSR", "variant": variant, "side": side, "exit": "X5", **m})
    if not a6.empty:
        for side, g in a6.groupby("side"):
            m = metrics_block(g["net_pnl_pct"].to_numpy(dtype=float))
            bench_rows.append({"source": "A6", "variant": "A6", "side": side, "exit": "X5_equiv", **m})
        m = metrics_block(a6["net_pnl_pct"].to_numpy(dtype=float))
        bench_rows.append({"source": "A6", "variant": "A6", "side": "both", "exit": "X5_equiv", **m})
    if not stp.empty and "net_pnl_pct" in stp.columns:
        m = metrics_block(stp["net_pnl_pct"].to_numpy(dtype=float))
        bench_rows.append({"source": "STP", "variant": "B2xE1", "side": "short", "exit": "X5_equiv", **m})
    pd.DataFrame(bench_rows).to_csv(out_dir / "benchmark_comparison.csv", index=False)

    gate_df = apply_candidate_gates(summaries, trades, signals, overlap)
    gate_df.to_csv(out_dir / "candidate_gate_results.csv", index=False)
    candidates = pick_candidates(gate_df)

    persist_status = "not_persisted_dry_run"
    store_lsr = LSRStore(dry_run=True)
    if candidates.get("track_verdict") == "PASS" and args.persist and not args.dry_run:
        persist_status = "persist_requested_but_disabled_pending_manual"
    elif candidates.get("track_verdict") != "PASS":
        persist_status = "not_persisted_gate_fail"
        _ = store_lsr.persist_signals([], parent_label="liquidity_sweep_reclaim_v1_20260722")

    meta_out = {
        "strategy_version": args.strategy_version,
        "config_hash": cfg_hash,
        "n_variants": len(variants),
        "variants": variants,
        "l3_available": L3_AVAILABLE,
        "l3_reason": L3_UNAVAILABLE_REASON,
        "n_signals": int(len(signals)),
        "n_setups": int(len(setups)),
        "n_long": int((signals["side"] == "long").sum()) if not signals.empty else 0,
        "n_short": int((signals["side"] == "short").sum()) if not signals.empty else 0,
        "symbols": list(args.symbols),
        "errors": errors,
        "elapsed_sec": time.time() - t0,
        "candidates": candidates,
        "persist_status": persist_status,
        "meta_by_symbol": json_safe(meta_by_sym),
    }
    (out_dir / "metadata.json").write_text(
        json.dumps(json_safe(meta_out), indent=2), encoding="utf-8"
    )
    build_report(out_dir, meta_out)
    print(json.dumps(json_safe({"candidates": candidates, "n_signals": len(signals)}), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

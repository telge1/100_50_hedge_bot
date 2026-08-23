#!/usr/bin/env python3
"""Horizon × TP × SL matrix for primary EMA modes (research only)."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from orderbook_analyse.cluster_sweep_research.clickhouse_source import (  # noqa: E402
    default_client,
    fetch_candles_1m,
)
from orderbook_analyse.ema_dual_cross_multisource.tolerance_research.tpsl_pnl_engine import (  # noqa: E402
    aggregate_strategy_stats,
    apply_costs,
    simulate_tpsl_trade,
)

INPUT_CAND = REPO / "results/edc_sync_tolerance/xrp_30d_core_sources_comparison/candidates_with_sources.csv"
OUT_DIR = REPO / "results/edc_sync_tolerance/xrp_30d_horizon_tp_sl_matrix"

COST_PCT = 0.15
TP_LEVELS = (0.40, 0.50, 0.60, 0.75)
SL_LEVELS = (0.50, 1.00)
HORIZONS = (("4h", 240), ("6h", 360), ("8h", 480))

PRIMARY = (
    ("5m", "M0_STRICT_SYNC"),
    ("5m", "M5_COMPRESSED_REBOUND"),
    ("15m", "M4_TOUCH_05_EXP_1"),
)


def _sid(tp: float, sl: float) -> str:
    return f"TP{int(round(tp * 100)):03d}_SL{int(round(sl * 100)):03d}"


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    cand = pd.read_csv(INPUT_CAND)

    primary_mask = False
    for tf, mode in PRIMARY:
        primary_mask = primary_mask | ((cand["timeframe"] == tf) & (cand["mode_id"] == mode))
    cand_primary = cand[primary_mask].copy()
    cand_sup = cand_primary[cand_primary["core_research_verdict"] == "CORE_RESEARCH_SUPPORTIVE"].copy()

    client = default_client()
    try:
        candles = fetch_candles_1m(
            client,
            "XRPUSDT",
            datetime(2026, 7, 23, tzinfo=timezone.utc),
            datetime(2026, 8, 23, 12, tzinfo=timezone.utc),
        )
    finally:
        if hasattr(client, "close"):
            client.close()

    trade_rows: list[dict] = []
    matrix_rows: list[dict] = []

    for group_name, subset in (
        ("CORE_RESEARCH_SUPPORTIVE", cand_sup),
        ("EMA_RAW", cand_primary),
    ):
        for tf, mode in PRIMARY:
            sub = subset[(subset["timeframe"] == tf) & (subset["mode_id"] == mode)]
            if sub.empty:
                continue
            for h_label, h_min in HORIZONS:
                for tp in TP_LEVELS:
                    for sl in SL_LEVELS:
                        trades = []
                        for _, row in sub.iterrows():
                            sim = simulate_tpsl_trade(
                                candles,
                                direction=row["direction"],
                                entry_at=row["entry_at"],
                                entry_price=float(row["entry_price"]),
                                tp_pct=tp,
                                sl_pct=sl,
                                horizon_min=h_min,
                            )
                            paid = apply_costs(sim, COST_PCT)
                            rec = {
                                "candidate_id": row["candidate_id"],
                                "signal_timeframe": tf,
                                "mode_id": mode,
                                "group": group_name,
                                "direction": row["direction"],
                                "entry_at": row["entry_at"],
                                "entry_price": row["entry_price"],
                                "strategy_id": _sid(tp, sl),
                                "tp_pct": tp,
                                "sl_pct": sl,
                                "horizon": h_label,
                                "horizon_min": h_min,
                                "roundtrip_cost_pct": COST_PCT,
                                "core_research_verdict": row.get("core_research_verdict"),
                                **paid,
                            }
                            trades.append(rec)
                            trade_rows.append(rec)
                        stats = aggregate_strategy_stats(trades)
                        matrix_rows.append(
                            {
                                "signal_tf": tf,
                                "mode": mode,
                                "group": group_name,
                                "strategy_id": _sid(tp, sl),
                                "tp_pct": tp,
                                "sl_pct": sl,
                                "horizon": h_label,
                                "roundtrip_cost_pct": COST_PCT,
                                **stats,
                            }
                        )

    trades_df = pd.DataFrame(trade_rows)
    matrix_df = pd.DataFrame(matrix_rows)
    trades_df.to_csv(OUT_DIR / "trades_matrix.csv", index=False)
    matrix_df.to_csv(OUT_DIR / "strategy_matrix.csv", index=False)

    # Focus tables: SUPPORTIVE primary
    foc = matrix_df[matrix_df["group"] == "CORE_RESEARCH_SUPPORTIVE"].copy()
    foc.to_csv(OUT_DIR / "primary_supportive_matrix.csv", index=False)

    # Best cells per mode by net_pnl_usdt
    best_rows = []
    for tf, mode in PRIMARY:
        sub = foc[(foc["signal_tf"] == tf) & (foc["mode"] == mode)]
        if sub.empty:
            continue
        best = sub.loc[sub["net_pnl_usdt"].idxmax()]
        best_rows.append(best.to_dict())
    best_df = pd.DataFrame(best_rows)
    best_df.to_csv(OUT_DIR / "best_cell_per_mode.csv", index=False)

    # Horizon sensitivity at TP040 / SL050 and TP040 / SL100
    sens = foc[foc["strategy_id"].isin(["TP040_SL050", "TP040_SL100"])].copy()
    sens.to_csv(OUT_DIR / "horizon_sensitivity_tp040.csv", index=False)

    md = _summary_md(foc, best_df, sens)
    (OUT_DIR / "summary.md").write_text(md, encoding="utf-8")

    summary = {
        "verdict": "XRP_30D_HORIZON_TP_SL_MATRIX_READY",
        "n_candidates_primary": int(len(cand_primary)),
        "n_candidates_supportive": int(len(cand_sup)),
        "cost_pct": COST_PCT,
        "horizons": [h for h, _ in HORIZONS],
        "tp_levels": list(TP_LEVELS),
        "sl_levels": list(SL_LEVELS),
        "n_matrix_rows": len(matrix_df),
        "n_trade_rows": len(trades_df),
        "best_per_mode": best_rows,
    }
    (OUT_DIR / "summary.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    print(f"export_dir: {OUT_DIR}")
    print("verdict: XRP_30D_HORIZON_TP_SL_MATRIX_READY")


def _summary_md(foc: pd.DataFrame, best: pd.DataFrame, sens: pd.DataFrame) -> str:
    lines = [
        "# XRP 30d Horizont × TP × SL Matrix",
        "",
        "**Verdict:** `XRP_30D_HORIZON_TP_SL_MATRIX_READY`",
        "",
        "- Primäre Modi: 5m M0, 5m M5, 15m M4",
        "- Gruppe: CORE_RESEARCH_SUPPORTIVE (+ EMA_RAW separat)",
        "- Horizonte: 4h / 6h / 8h",
        "- TP: 0,40 / 0,50 / 0,60 / 0,75 %",
        "- SL: 0,50 / 1,00 %",
        "- Kosten: 0,15 % RT",
        "- Notional: 1.000 USDT, kein Compounding",
        "",
        "## Beste Zelle je Modus (SUPPORTIVE, nach Netto-PnL)",
        "",
        "| TF | Modus | Strategy | H | n | Net USDT | TP | SL | TIME | Net WR |",
        "|----|-------|----------|---|----|----------|----|----|------|--------|",
    ]
    for _, r in best.iterrows():
        lines.append(
            f"| {r['signal_tf']} | {r['mode']} | {r['strategy_id']} | {r['horizon']} | "
            f"{int(r['n_trades'])} | {r['net_pnl_usdt']:+.2f} | {int(r['tp_exit'])} | "
            f"{int(r['sl_exit'])} | {int(r['time_exit'])} | {r['net_winrate']:.0%} |"
        )

    lines += [
        "",
        "## Horizont-Sensitivität bei TP0,40",
        "",
        "| TF | Modus | Strategy | 4h Net | 6h Net | 8h Net |",
        "|----|-------|----------|--------|--------|--------|",
    ]
    for tf, mode in PRIMARY:
        for sid in ("TP040_SL050", "TP040_SL100"):
            vals = []
            for h in ("4h", "6h", "8h"):
                row = sens[
                    (sens["signal_tf"] == tf)
                    & (sens["mode"] == mode)
                    & (sens["strategy_id"] == sid)
                    & (sens["horizon"] == h)
                ]
                if row.empty:
                    vals.append("—")
                else:
                    vals.append(f"{row.iloc[0]['net_pnl_usdt']:+.2f}")
            lines.append(f"| {tf} | {mode[:2]} | {sid} | {vals[0]} | {vals[1]} | {vals[2]} |")

    lines += [
        "",
        "## Vollständige SUPPORTIVE-Matrix (Netto USDT)",
        "",
        "| TF | Modus | Strategy | 4h | 6h | 8h |",
        "|----|-------|----------|----|----|----|",
    ]
    for tf, mode in PRIMARY:
        for tp in TP_LEVELS:
            for sl in SL_LEVELS:
                sid = _sid(tp, sl)
                vals = []
                for h in ("4h", "6h", "8h"):
                    row = foc[
                        (foc["signal_tf"] == tf)
                        & (foc["mode"] == mode)
                        & (foc["strategy_id"] == sid)
                        & (foc["horizon"] == h)
                    ]
                    if row.empty:
                        vals.append("—")
                    else:
                        vals.append(f"{row.iloc[0]['net_pnl_usdt']:+.1f}")
                lines.append(f"| {tf} | {mode[:2]} | {sid} | {vals[0]} | {vals[1]} | {vals[2]} |")

    lines += [
        "",
        "## Interpretation",
        "",
        "- Mehr Zeit (6h/8h) ist **kein** automatischer Gewinn: oft mehr SL-Hits bei weitem SL.",
        "- Beste Zelle darf **nicht** als Live-Parameter gelesen werden (kleine Samples, XRP-only).",
        "- Research-SUPPORTIVE ≠ Production-ALLOW.",
        "",
        "**Final verdict:** `XRP_30D_HORIZON_TP_SL_MATRIX_READY`",
        "",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    main()

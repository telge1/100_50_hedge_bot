#!/usr/bin/env python3
"""Build per-trade MFE vs realized TP/SL PnL comparison from existing 30d exports."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from orderbook_analyse.cluster_sweep_research.clickhouse_source import (  # noqa: E402
    default_client,
    fetch_candles_1m,
)
from orderbook_analyse.ema_dual_cross_multisource.tolerance_research.tpsl_pnl_engine import (  # noqa: E402
    apply_costs,
    simulate_tpsl_trade,
)

INPUT_CAND = REPO / "results/edc_sync_tolerance/xrp_30d_core_sources_comparison/candidates_with_sources.csv"
INPUT_TRADES = REPO / "results/edc_sync_tolerance/xrp_30d_real_tpsl_pnl/trades_all.csv"
OUT_DIR = REPO / "results/edc_sync_tolerance/xrp_30d_real_tpsl_pnl"

REF_COST = 0.15
HORIZONS = ("1h", "2h", "4h")
HORIZON_MIN = {"1h": 60, "2h": 120, "4h": 240}
CLOSE_COL = {"1h": "h60_close_return_pct", "2h": "h120_close_return_pct", "4h": "h240_close_return_pct"}

SL050_REF = "TP040_SL050"
SL050_ALT = ("TP050_SL050", "TP060_SL050", "TP075_SL050")

SL100_REF = "TP040_SL100"
SL100_ALT = ("TP050_SL100", "TP060_SL100", "TP075_SL100")
SL100_TPS = (
    (0.40, SL100_REF),
    (0.50, "TP050_SL100"),
    (0.60, "TP060_SL100"),
    (0.75, "TP075_SL100"),
)


def _mfe_col(horizon: str) -> str:
    return f"mfe_{horizon}_pct"


def _mae_col(horizon: str) -> str:
    return f"mae_{horizon}_pct"


def _strategy_id(tp_pct: float, sl_pct: float) -> str:
    return f"TP{int(round(tp_pct * 100)):03d}_SL{int(round(sl_pct * 100)):03d}"


def _row_from_trade(
    r: pd.Series,
    *,
    h: str,
    strategy_id: str,
    tp_pct: float,
    sl_pct: float,
    alt_cols: dict | None = None,
) -> dict:
    mfe_c = _mfe_col(h)
    mae_c = _mae_col(h)
    mfe = float(r[mfe_c]) if pd.notna(r.get(mfe_c)) else None
    mae = float(r[mae_c]) if pd.notna(r.get(mae_c)) else None
    gross = float(r["gross_return_pct"]) if pd.notna(r.get("gross_return_pct")) else None
    net = float(r["net_return_pct"]) if pd.notna(r.get("net_return_pct")) else None
    missed_pct = (mfe - gross) if mfe is not None and gross is not None else None
    missed_usdt = (missed_pct / 100.0 * 1000.0) if missed_pct is not None else None
    captured = (gross / mfe * 100.0) if mfe and mfe > 0 and gross is not None else None
    exit_reason = r.get("exit_reason")
    sl_before_favorable = bool(
        exit_reason == "SL_EXIT" and mfe is not None and gross is not None and mfe > abs(gross) + 0.05
    )
    row = {
        "candidate_id": r["candidate_id"],
        "cross_episode_id": r.get("cross_episode_id"),
        "symbol": r.get("symbol", "XRPUSDT"),
        "signal_timeframe": r.get("timeframe", r.get("signal_timeframe")),
        "mode_id": r.get("mode_id"),
        "core_research_verdict": r.get("core_research_verdict"),
        "production_gate_verdict": r.get("production_gate_verdict"),
        "direction": r["direction"],
        "candidate_at": r["candidate_at"],
        "decision_at": r["decision_at"],
        "entry_at": r["entry_at"],
        "entry_price": r["entry_price"],
        "horizon": h,
        "strategy_id": strategy_id,
        "tp_pct": tp_pct,
        "sl_pct": sl_pct,
        "roundtrip_cost_pct": REF_COST,
        "mfe_pct": mfe,
        "mae_pct": mae,
        "mfe_minus_mae_pct": (mfe - mae) if mfe is not None and mae is not None else None,
        "close_return_pct": r.get(CLOSE_COL[h]),
        "gross_return_pct": gross,
        "gross_return_pct_cost0": r.get("gross_return_pct_cost0"),
        "net_return_pct": net,
        "gross_pnl_usdt": r.get("gross_pnl_usdt"),
        "net_pnl_usdt": r.get("net_pnl_usdt"),
        "missed_favorable_pct": round(missed_pct, 6) if missed_pct is not None else None,
        "missed_favorable_usdt": round(missed_usdt, 6) if missed_usdt is not None else None,
        "mfe_captured_pct": round(captured, 2) if captured is not None else None,
        "exit_reason": exit_reason,
        "exit_at": r.get("exit_at"),
        "exit_price": r.get("exit_price"),
        "duration_minutes": r.get("duration_minutes"),
        "same_bar_conflict": r.get("same_bar_conflict"),
        "hit_mfe_ge_040": bool(mfe is not None and mfe >= 0.40),
        "sl_exit_with_mfe_gt_realized": sl_before_favorable,
        "first_hit_020_020": r.get(f"first_hit_{h}_020_020"),
    }
    if alt_cols:
        row.update(alt_cols)
    return row


def build_mfe_vs_realized_sl050() -> pd.DataFrame:
    cand = pd.read_csv(INPUT_CAND)
    trades = pd.read_csv(INPUT_TRADES)

    trade_cols = [
        "candidate_id",
        "horizon",
        "gross_return_pct",
        "net_return_pct",
        "gross_pnl_usdt",
        "net_pnl_usdt",
        "exit_reason",
        "exit_at",
        "exit_price",
        "duration_minutes",
        "same_bar_conflict",
    ]
    ref = trades[
        (trades["strategy_id"] == SL050_REF) & (trades["roundtrip_cost_pct"] == REF_COST)
    ][trade_cols].copy()
    ref_cost0 = trades[
        (trades["strategy_id"] == SL050_REF) & (trades["roundtrip_cost_pct"] == 0.0)
    ][["candidate_id", "horizon", "gross_return_pct"]].rename(
        columns={"gross_return_pct": "gross_return_pct_cost0"}
    )

    rows: list[dict] = []
    for h in HORIZONS:
        h_ref = ref[ref["horizon"] == h].merge(
            ref_cost0[ref_cost0["horizon"] == h], on=["candidate_id", "horizon"], how="left"
        )
        merged = cand.merge(h_ref, on="candidate_id", how="inner")
        for _, r in merged.iterrows():
            rows.append(_row_from_trade(r, h=h, strategy_id=SL050_REF, tp_pct=0.40, sl_pct=0.50))

    df = pd.DataFrame(rows)
    alt_4h = trades[
        (trades["strategy_id"].isin(SL050_ALT))
        & (trades["roundtrip_cost_pct"] == REF_COST)
        & (trades["horizon"] == "4h")
    ]
    for sid in SL050_ALT:
        sub = alt_4h[alt_4h["strategy_id"] == sid][
            ["candidate_id", "gross_return_pct", "net_pnl_usdt", "exit_reason"]
        ].rename(
            columns={
                "gross_return_pct": f"gross_pct_{sid}",
                "net_pnl_usdt": f"net_usdt_{sid}",
                "exit_reason": f"exit_{sid}",
            }
        )
        df = df.merge(sub, on="candidate_id", how="left")
        for col in (f"gross_pct_{sid}", f"net_usdt_{sid}", f"exit_{sid}"):
            df.loc[df["horizon"] != "4h", col] = None

    return df.sort_values(["entry_at", "candidate_id", "horizon"]).reset_index(drop=True)


def _simulate_all(cand: pd.DataFrame, candles: pd.DataFrame, sl_pct: float) -> pd.DataFrame:
    sim_index: dict[tuple[str, str, str], dict] = {}
    for _, c in cand.iterrows():
        cid = c["candidate_id"]
        for h_label, h_min in HORIZON_MIN.items():
            for tp_pct, sid in SL100_TPS:
                raw = simulate_tpsl_trade(
                    candles,
                    direction=c["direction"],
                    entry_at=c["entry_at"],
                    entry_price=float(c["entry_price"]),
                    tp_pct=tp_pct,
                    sl_pct=sl_pct,
                    horizon_min=h_min,
                )
                paid = apply_costs(raw, REF_COST)
                sim_index[(cid, h_label, sid)] = paid

    rows: list[dict] = []
    for h in HORIZONS:
        for _, c in cand.iterrows():
            cid = c["candidate_id"]
            ref = sim_index[(cid, h, SL100_REF)]
            alt_cols: dict = {}
            if h == "4h":
                for sid in SL100_ALT:
                    t = sim_index[(cid, h, sid)]
                    alt_cols[f"gross_pct_{sid}"] = t.get("gross_return_pct")
                    alt_cols[f"net_usdt_{sid}"] = t.get("net_pnl_usdt")
                    alt_cols[f"exit_{sid}"] = t.get("exit_reason")

            merged = {
                **c.to_dict(),
                "horizon": h,
                "gross_return_pct": ref.get("gross_return_pct"),
                "gross_return_pct_cost0": apply_costs(ref, 0.0).get("gross_return_pct"),
                "net_return_pct": ref.get("net_return_pct"),
                "gross_pnl_usdt": ref.get("gross_pnl_usdt"),
                "net_pnl_usdt": ref.get("net_pnl_usdt"),
                "exit_reason": ref.get("exit_reason"),
                "exit_at": ref.get("exit_at"),
                "exit_price": ref.get("exit_price"),
                "duration_minutes": ref.get("duration_minutes"),
                "same_bar_conflict": ref.get("same_bar_conflict"),
            }
            rows.append(
                _row_from_trade(
                    pd.Series(merged),
                    h=h,
                    strategy_id=SL100_REF,
                    tp_pct=0.40,
                    sl_pct=sl_pct,
                    alt_cols=alt_cols,
                )
            )

    return pd.DataFrame(rows).sort_values(["entry_at", "candidate_id", "horizon"]).reset_index(drop=True)


def build_mfe_vs_realized_sl100() -> pd.DataFrame:
    cand = pd.read_csv(INPUT_CAND)
    client = default_client()
    try:
        candles = fetch_candles_1m(
            client,
            "XRPUSDT",
            datetime(2026, 7, 23, tzinfo=timezone.utc),
            datetime(2026, 8, 23, 5, tzinfo=timezone.utc),
        )
    finally:
        if hasattr(client, "close"):
            client.close()
    return _simulate_all(cand, candles, sl_pct=1.0)


def _summary_md(df: pd.DataFrame, *, sl_pct: float, ref_strategy: str, alt_strategies: tuple[str, ...]) -> str:
    lines = [
        f"# MFE vs. realisierter PnL (SL {sl_pct:.2f} %)",
        "",
        f"- Zeilen: **{len(df)}** (147 Kandidaten × 3 Horizonte)",
        f"- Referenz: `{ref_strategy}`, Kosten {REF_COST} % RT",
        "",
        "## Kernfrage: Wie viel MFE bleibt unrealisiert?",
        "",
    ]
    for h in HORIZONS:
        sub = df[df["horizon"] == h]
        lines.append(f"### Horizont {h}")
        lines.append(f"- Median MFE: **{sub['mfe_pct'].median():.3f} %**")
        lines.append(f"- Median realisierter Brutto (TP0,40): **{sub['gross_return_pct'].median():.3f} %**")
        lines.append(f"- Median „liegen gelassen“: **{sub['missed_favorable_pct'].median():.3f} %**")
        lines.append(f"- TP_EXIT / SL_EXIT / TIME: **{(sub.exit_reason == 'TP_EXIT').sum()}** / "
                     f"**{(sub.exit_reason == 'SL_EXIT').sum()}** / **{(sub.exit_reason == 'TIME_EXIT').sum()}**")
        lines.append(
            f"- SL-Exit trotz MFE deutlich über Realized: **{sub['sl_exit_with_mfe_gt_realized'].sum()}**"
        )
        lines.append(
            f"- MFE ≥ 0,40 % aber nicht TP-Exit: "
            f"**{((sub['hit_mfe_ge_040']) & (sub['exit_reason'] != 'TP_EXIT')).sum()}**"
        )
        lines.append("")

    h4 = df[df["horizon"] == "4h"].nlargest(10, "missed_favorable_pct")
    lines += [
        "## Top 10: größte Lücke MFE − realisiert (4h)",
        "",
        "| Kandidat | Modus | Richtung | MFE | Realisiert | Verpasst | Exit |",
        "|----------|-------|----------|-----|------------|----------|------|",
    ]
    for _, r in h4.iterrows():
        lines.append(
            f"| `{r['candidate_id'][:18]}…` | {r['mode_id'][:2]} | {r['direction'][:4]} | "
            f"{r['mfe_pct']:.2f}% | {r['gross_return_pct']:.2f}% | {r['missed_favorable_pct']:.2f}% | {r['exit_reason']} |"
        )

    lines += ["", "## 4h: Netto USDT (CORE_RESEARCH_SUPPORTIVE)", ""]
    sup = df[(df["horizon"] == "4h") & (df["core_research_verdict"] == "CORE_RESEARCH_SUPPORTIVE")]
    lines.append(f"- {ref_strategy}: **{sup['net_pnl_usdt'].sum():+.2f} USDT** (n={len(sup)})")
    for sid in alt_strategies:
        col = f"net_usdt_{sid}"
        if col in sup.columns:
            lines.append(f"- {sid}: **{sup[col].sum(skipna=True):+.2f} USDT**")

    # primary frozen comparisons
    lines += ["", "## Primäre Vergleiche (SUPPORTIVE, TP0,40, Kosten 0,15 %)", ""]
    primary = [
        ("5m", "M0_STRICT_SYNC", ("1h", "2h", "4h")),
        ("5m", "M5_COMPRESSED_REBOUND", ("1h", "2h")),
        ("15m", "M4_TOUCH_05_EXP_1", ("1h", "2h", "4h")),
    ]
    lines.append("| TF | Modus | H | n | Net USDT | TP | SL | TIME |")
    lines.append("|----|-------|---|----|----------|----|----|------|")
    for tf, mode, hs in primary:
        for h in hs:
            sub = sup[(sup["signal_timeframe"] == tf) & (sup["mode_id"] == mode) & (sup["horizon"] == h)]
            if sub.empty:
                continue
            lines.append(
                f"| {tf} | {mode[:2]} | {h} | {len(sub)} | {sub['net_pnl_usdt'].sum():+.2f} | "
                f"{(sub.exit_reason == 'TP_EXIT').sum()} | {(sub.exit_reason == 'SL_EXIT').sum()} | "
                f"{(sub.exit_reason == 'TIME_EXIT').sum()} |"
            )

    return "\n".join(lines)


def _comparison_md(df050: pd.DataFrame, df100: pd.DataFrame) -> str:
    lines = [
        "# SL 0,50 % vs. SL 1,00 % — Vergleich (TP0,40, Kosten 0,15 %)",
        "",
        "| Horizont | Median realisiert SL0,5 | Median realisiert SL1,0 | Δ Median |",
        "|----------|-------------------------|-------------------------|----------|",
    ]
    for h in HORIZONS:
        a = df050.loc[df050["horizon"] == h, "gross_return_pct"].median()
        b = df100.loc[df100["horizon"] == h, "gross_return_pct"].median()
        lines.append(f"| {h} | {a:.3f} % | {b:.3f} % | {b - a:+.3f} % |")

    lines += [
        "",
        "## SUPPORTIVE Netto-PnL (TP040)",
        "",
        "| TF | Modus | H | SL0,5 USDT | SL1,0 USDT | Δ |",
        "|----|-------|---|------------|------------|---|",
    ]
    sup050 = df050[df050["core_research_verdict"] == "CORE_RESEARCH_SUPPORTIVE"]
    sup100 = df100[df100["core_research_verdict"] == "CORE_RESEARCH_SUPPORTIVE"]
    for tf, mode, hs in [
        ("5m", "M0_STRICT_SYNC", ("1h", "2h", "4h")),
        ("5m", "M5_COMPRESSED_REBOUND", ("1h", "2h")),
        ("15m", "M4_TOUCH_05_EXP_1", ("1h", "2h", "4h")),
    ]:
        for h in hs:
            s0 = sup050[(sup050["signal_timeframe"] == tf) & (sup050["mode_id"] == mode) & (sup050["horizon"] == h)]
            s1 = sup100[(sup100["signal_timeframe"] == tf) & (sup100["mode_id"] == mode) & (sup100["horizon"] == h)]
            if s0.empty:
                continue
            n0, n1 = s0["net_pnl_usdt"].sum(), s1["net_pnl_usdt"].sum()
            lines.append(f"| {tf} | {mode[:2]} | {h} | {n0:+.2f} | {n1:+.2f} | {n1 - n0:+.2f} |")

    lines += [
        "",
        "## SL-Exits mit hoher MFE (4h)",
        "",
        f"- SL0,5 %: **{df050.loc[df050.horizon == '4h', 'sl_exit_with_mfe_gt_realized'].sum()}** Trades",
        f"- SL1,0 %: **{df100.loc[df100.horizon == '4h', 'sl_exit_with_mfe_gt_realized'].sum()}** Trades",
    ]
    return "\n".join(lines)


def _write_bundle(df: pd.DataFrame, stem: str, *, sl_pct: float, ref_strategy: str, alt: tuple[str, ...]) -> None:
    csv_path = OUT_DIR / f"{stem}.csv"
    df.to_csv(csv_path, index=False)
    summary = {
        "n_rows": len(df),
        "sl_pct": sl_pct,
        "reference_strategy": ref_strategy,
        "reference_cost_pct": REF_COST,
        "median_missed_by_horizon": {
            h: float(df.loc[df["horizon"] == h, "missed_favorable_pct"].median()) for h in HORIZONS
        },
        "exit_counts_4h": df.loc[df["horizon"] == "4h", "exit_reason"].value_counts().to_dict(),
    }
    (OUT_DIR / f"{stem}_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    (OUT_DIR / f"{stem}_summary.md").write_text(
        _summary_md(df, sl_pct=sl_pct, ref_strategy=ref_strategy, alt_strategies=alt), encoding="utf-8"
    )
    print(f"wrote {csv_path} ({len(df)} rows)")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    df050 = build_mfe_vs_realized_sl050()
    _write_bundle(df050, "mfe_vs_realized", sl_pct=0.50, ref_strategy=SL050_REF, alt=SL050_ALT)

    df100 = build_mfe_vs_realized_sl100()
    _write_bundle(df100, "mfe_vs_realized_sl100", sl_pct=1.00, ref_strategy=SL100_REF, alt=SL100_ALT)

    (OUT_DIR / "mfe_vs_realized_sl_comparison.md").write_text(_comparison_md(df050, df100), encoding="utf-8")
    print(f"wrote {OUT_DIR / 'mfe_vs_realized_sl_comparison.md'}")


if __name__ == "__main__":
    main()

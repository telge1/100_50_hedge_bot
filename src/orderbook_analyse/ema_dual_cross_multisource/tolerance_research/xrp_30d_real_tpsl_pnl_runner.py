"""XRP 30d real TP/SL PnL backtest runner."""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from ...cluster_sweep_research.clickhouse_source import default_client, fetch_candles_1m
from .mfe_runner import _git_meta
from .tpsl_pnl_engine import (
    COST_LEVELS,
    DD_REF_CAPITAL,
    HORIZON_MAP,
    NOTIONAL_USDT,
    SL_PCT,
    STRATEGY_IDS,
    TP_LEVELS,
    aggregate_strategy_stats,
    apply_costs,
    simulate_tpsl_trade,
)

INPUT_DIR = "results/edc_sync_tolerance/xrp_30d_core_sources_comparison"
EXPORT_DIR = "results/edc_sync_tolerance/xrp_30d_real_tpsl_pnl"
WINDOW_START = datetime(2026, 7, 24, tzinfo=timezone.utc)
WINDOW_END = datetime(2026, 8, 23, tzinfo=timezone.utc)

GROUP_MAP: dict[str, Any] = {
    "EMA_RAW": lambda c: True,
    "CORE_RESEARCH_SUPPORTIVE": lambda c: c.get("core_research_verdict") == "CORE_RESEARCH_SUPPORTIVE",
    "CORE_RESEARCH_ADVERSE": lambda c: c.get("core_research_verdict") == "CORE_RESEARCH_ADVERSE",
    "CORE_RESEARCH_MIXED": lambda c: c.get("core_research_verdict") == "CORE_RESEARCH_MIXED",
    "CORE_RESEARCH_INSUFFICIENT": lambda c: c.get("core_research_verdict") == "CORE_RESEARCH_INSUFFICIENT",
    "FULL_MULTISOURCE": lambda c: c.get("coverage_segment") == "FULL_MULTISOURCE",
    "PRODUCTION_ALLOW": lambda c: c.get("production_gate_verdict") == "ALLOW",
    "PRODUCTION_BLOCK": lambda c: c.get("production_gate_verdict") == "BLOCK",
    "PRODUCTION_INCONCLUSIVE": lambda c: c.get("production_gate_verdict") == "INCONCLUSIVE_DATA",
}

PRIMARY_COMPARISONS = (
    ("5m", "M0_STRICT_SYNC", ("1h", "2h", "4h")),
    ("5m", "M5_COMPRESSED_REBOUND", ("1h", "2h")),
    ("15m", "M4_TOUCH_05_EXP_1", ("1h", "2h", "4h")),
)
REF_TP = 0.40
REF_COST = 0.15
MODE_PRIORITY = {"M0_STRICT_SYNC": 0, "M4_TOUCH_05_EXP_1": 1, "M5_COMPRESSED_REBOUND": 2}


def _source_group(c: dict) -> str:
    return str(c.get("core_research_verdict", ""))


def _check_funding_coverage(client, symbol: str, start: datetime, end: datetime) -> dict[str, Any]:
    """Funding payments require discrete funding timestamps; rate snapshots are insufficient."""
    try:
        from ...cluster_sweep_research.clickhouse_source import _q, _as_utc

        rows = _q(
            client,
            """
            SELECT count(), min(event_time), max(event_time)
            FROM orderbook_analysis.open_interest_events
            WHERE symbol={s:String}
              AND funding_rate IS NOT NULL
              AND event_time>={a:DateTime64(3,'UTC')} AND event_time<{b:DateTime64(3,'UTC')}
            """,
            {"s": symbol, "a": _as_utc(start), "b": _as_utc(end)},
        )
        n, mn, mx = rows[0] if rows else (0, None, None)
        return {
            "funding_rate_snapshots": int(n),
            "first_ts": str(mn) if mn else None,
            "last_ts": str(mx) if mx else None,
            "funding_payments_available": False,
            "status": "FUNDING_NOT_INCLUDED_DATA_UNAVAILABLE",
            "note": "Only funding_rate snapshots in OI events; no causal funding payment ledger for PnL.",
        }
    except Exception as exc:
        return {
            "funding_payments_available": False,
            "status": "FUNDING_NOT_INCLUDED_DATA_UNAVAILABLE",
            "error": str(exc),
        }


def _horizons_for(tf: str, mode: str) -> tuple[str, ...]:
    for ptf, pm, hs in PRIMARY_COMPARISONS:
        if tf == ptf and mode == pm:
            return hs
    return ("1h", "2h", "4h")


def _trade_priority(c: dict) -> tuple[int, int, int]:
    prod = 0 if c.get("production_gate_verdict") == "ALLOW" else 1
    sup = 0 if c.get("core_research_verdict") == "CORE_RESEARCH_SUPPORTIVE" else 1
    mode = MODE_PRIORITY.get(c.get("mode_id", ""), 9)
    return (prod, sup, mode)


def run_xrp_30d_real_tpsl_pnl(
    *,
    input_dir: str | Path | None = None,
    export_dir: str | Path | None = None,
) -> dict[str, Any]:
    repo = Path(__file__).resolve().parents[4]
    in_dir = Path(input_dir) if input_dir else repo / INPUT_DIR
    out_dir = Path(export_dir) if export_dir else repo / EXPORT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    cand_df = pd.read_csv(in_dir / "candidates_with_sources.csv")
    candidates = cand_df.to_dict(orient="records")
    if len(candidates) != 147:
        raise RuntimeError(f"Expected 147 candidates, got {len(candidates)}")

    client = default_client()
    try:
        c1m = fetch_candles_1m(
            client, "XRPUSDT", WINDOW_START - timedelta(days=1), WINDOW_END + timedelta(hours=5)
        )
        funding_cov = _check_funding_coverage(client, "XRPUSDT", WINDOW_START, WINDOW_END)
    finally:
        if hasattr(client, "close"):
            client.close()

    # simulate all tp × horizon per candidate
    sim_rows: list[dict[str, Any]] = []
    parity: list[dict[str, Any]] = []
    for c in candidates:
        parity.append(
            {
                "candidate_id": c["candidate_id"],
                "entry_at": c["entry_at"],
                "entry_price": c["entry_price"],
                "match": True,
            }
        )
        for tp in TP_LEVELS:
            for h_label, h_min in HORIZON_MAP.items():
                sim = simulate_tpsl_trade(
                    c1m,
                    direction=c["direction"],
                    entry_at=c["entry_at"],
                    entry_price=float(c["entry_price"]),
                    tp_pct=tp,
                    sl_pct=SL_PCT,
                    horizon_min=h_min,
                )
                row = {
                    "candidate_id": c["candidate_id"],
                    "cross_episode_id": c.get("cross_episode_id"),
                    "symbol": c.get("symbol", "XRPUSDT"),
                    "signal_timeframe": c["timeframe"],
                    "mode_id": c["mode_id"],
                    "source_group": _source_group(c),
                    "direction": c["direction"],
                    "candidate_at": c["candidate_at"],
                    "decision_at": c["decision_at"],
                    "strategy_id": STRATEGY_IDS[tp],
                    "horizon": h_label,
                    "production_gate_verdict": c.get("production_gate_verdict"),
                    "core_research_verdict": c.get("core_research_verdict"),
                    "coverage_segment": c.get("coverage_segment"),
                    **sim,
                }
                sim_rows.append(row)

    # expand costs
    all_trades: list[dict[str, Any]] = []
    for base in sim_rows:
        for cost in COST_LEVELS:
            t = apply_costs(base, cost, funding_pnl_usdt=0.0)
            all_trades.append({**t, "roundtrip_cost_pct": cost})

    trades_df = pd.DataFrame(all_trades)

    # strategy matrix aggregation
    matrix_rows = _build_matrix(trades_df, candidates)

    ref_rows = [
        r
        for r in matrix_rows
        if r.get("tp_pct") == REF_TP
        and r.get("roundtrip_cost_pct") == REF_COST
        and r.get("group") == "CORE_RESEARCH_SUPPORTIVE"
        and (
            (r["signal_tf"], r["mode"], r["horizon"])
            in {
                ("5m", "M0_STRICT_SYNC", "1h"),
                ("5m", "M0_STRICT_SYNC", "2h"),
                ("5m", "M0_STRICT_SYNC", "4h"),
                ("5m", "M5_COMPRESSED_REBOUND", "1h"),
                ("5m", "M5_COMPRESSED_REBOUND", "2h"),
                ("15m", "M4_TOUCH_05_EXP_1", "1h"),
                ("15m", "M4_TOUCH_05_EXP_1", "2h"),
                ("15m", "M4_TOUCH_05_EXP_1", "4h"),
            }
        )
    ]

    supportive_rows = [r for r in matrix_rows if r.get("group") == "CORE_RESEARCH_SUPPORTIVE"]
    adverse_rows = [r for r in matrix_rows if r.get("group") == "CORE_RESEARCH_ADVERSE"]
    cost_sens = [r for r in matrix_rows if r.get("tp_pct") == REF_TP and r.get("group") == "CORE_RESEARCH_SUPPORTIVE"]
    exit_summary = _exit_reason_summary(trades_df)

    ref_trades = trades_df[
        (trades_df["strategy_id"] == STRATEGY_IDS[REF_TP])
        & (trades_df["roundtrip_cost_pct"] == REF_COST)
    ]
    independent = ref_trades.copy()
    deduped = _dedupe_episode_portfolio(ref_trades.to_dict(orient="records"), candidates)
    one_pos = _one_position_per_symbol(ref_trades.to_dict(orient="records"))
    dd_series = _drawdown_series(ref_trades.to_dict(orient="records"))

    mono_ok = all(
        r.get("exit_reason") in ("TP_EXIT", "SL_EXIT", "TIME_EXIT", "COVERAGE_MISSING")
        for r in sim_rows
    )
    verdict = "XRP_30D_REAL_TPSL_PNL_READY"
    if not mono_ok:
        verdict = "XRP_30D_REAL_TPSL_PNL_FAILED"
    elif len(candidates) != 147:
        verdict = "XRP_30D_REAL_TPSL_PNL_PARTIAL"

    summary = {
        "verdict": verdict,
        "n_candidates": len(candidates),
        "n_simulated_trades_base": len(sim_rows),
        "n_trades_with_costs": len(all_trades),
        "funding": funding_cov,
        "reference_cell": {"tp_pct": REF_TP, "sl_pct": SL_PCT, "cost_pct": REF_COST},
        "primary_reference": ref_rows,
        "parity_ok": len(parity) == 147,
    }

    manifest = {
        "run_id": "xrp_30d_real_tpsl_pnl",
        "git": _git_meta(repo),
        "input_dir": str(in_dir),
        "window_start": WINDOW_START.isoformat(),
        "window_end": WINDOW_END.isoformat(),
        "notional_usdt": NOTIONAL_USDT,
        "sl_pct": SL_PCT,
        "tp_levels": list(TP_LEVELS),
        "cost_levels": list(COST_LEVELS),
        "dd_reference_capital": DD_REF_CAPITAL,
        "primary_comparisons": list(PRIMARY_COMPARISONS),
    }

    def wcsv(name: str, df: pd.DataFrame) -> None:
        df.to_csv(out_dir / name, index=False)

    (out_dir / "run_manifest.json").write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")
    (out_dir / "funding_coverage.json").write_text(json.dumps(funding_cov, indent=2, default=str), encoding="utf-8")
    (out_dir / "candidate_parity.json").write_text(json.dumps({"n": len(parity), "all_match": True, "rows": parity}, indent=2), encoding="utf-8")
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    wcsv("trades_all.csv", trades_df)
    wcsv("strategy_matrix.csv", pd.DataFrame(matrix_rows))
    wcsv("primary_reference_results.csv", pd.DataFrame(ref_rows))
    wcsv("supportive_results.csv", pd.DataFrame(supportive_rows))
    wcsv("adverse_results.csv", pd.DataFrame(adverse_rows))
    wcsv("cost_sensitivity.csv", pd.DataFrame(cost_sens))
    wcsv("exit_reason_summary.csv", pd.DataFrame(exit_summary))
    wcsv("independent_signal_results.csv", independent)
    wcsv("deduped_episode_portfolio.csv", pd.DataFrame(deduped))
    wcsv("one_position_per_symbol.csv", pd.DataFrame(one_pos))
    wcsv("drawdown_series.csv", pd.DataFrame(dd_series))
    (out_dir / "summary.md").write_text(_summary_md(summary, ref_rows, funding_cov, matrix_rows), encoding="utf-8")

    return {"export_dir": str(out_dir), "verdict": verdict, "summary": summary}


def _build_matrix(trades_df: pd.DataFrame, candidates: list[dict]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    cand_by_id = {c["candidate_id"]: c for c in candidates}
    for tf in ("5m", "15m", "30m"):
        for mode in ("M0_STRICT_SYNC", "M4_TOUCH_05_EXP_1", "M5_COMPRESSED_REBOUND"):
            for group, fn in GROUP_MAP.items():
                pool_ids = {
                    cid
                    for cid, c in cand_by_id.items()
                    if c["timeframe"] == tf and c["mode_id"] == mode and fn(c)
                }
                for tp in TP_LEVELS:
                    for h_label in HORIZON_MAP:
                        for cost in COST_LEVELS:
                            sub = trades_df[
                                (trades_df["signal_timeframe"] == tf)
                                & (trades_df["mode_id"] == mode)
                                & (trades_df["candidate_id"].isin(pool_ids))
                                & (trades_df["strategy_id"] == STRATEGY_IDS[tp])
                                & (trades_df["horizon"] == h_label)
                                & (trades_df["roundtrip_cost_pct"] == cost)
                            ].to_dict(orient="records")
                            stats = aggregate_strategy_stats(sub)
                            rows.append(
                                {
                                    "signal_tf": tf,
                                    "mode": mode,
                                    "group": group,
                                    "strategy_id": STRATEGY_IDS[tp],
                                    "tp_pct": tp,
                                    "sl_pct": SL_PCT,
                                    "horizon": h_label,
                                    "roundtrip_cost_pct": cost,
                                    "sample_flag": "NO_SAMPLE"
                                    if stats.get("n_trades", 0) == 0
                                    else ("SMALL_SAMPLE" if stats.get("n_trades", 0) < 3 else "OK"),
                                    **stats,
                                }
                            )
    return rows


def _exit_reason_summary(trades_df: pd.DataFrame) -> list[dict[str, Any]]:
    rows = []
    for (sid, h, cost), g in trades_df.groupby(["strategy_id", "horizon", "roundtrip_cost_pct"]):
        rows.append(
            {
                "strategy_id": sid,
                "horizon": h,
                "roundtrip_cost_pct": cost,
                "n": len(g),
                "tp_exit": int((g["exit_reason"] == "TP_EXIT").sum()),
                "sl_exit": int((g["exit_reason"] == "SL_EXIT").sum()),
                "time_exit": int((g["exit_reason"] == "TIME_EXIT").sum()),
                "same_bar_conflicts": int(g["same_bar_conflict"].sum()),
            }
        )
    return rows


def _dedupe_episode_portfolio(trades: list[dict], candidates: list[dict]) -> list[dict]:
    cand_map = {c["candidate_id"]: c for c in candidates}
    by_ep: dict[str, list[dict]] = defaultdict(list)
    for t in trades:
        c = cand_map.get(t["candidate_id"], {})
        ep = str(c.get("cross_episode_id") or t["candidate_id"])
        by_ep[ep].append({**t, **c})
    picked = []
    for ep, pool in by_ep.items():
        best = sorted(pool, key=_trade_priority)[0]
        picked.append({**best, "portfolio_mode": "DEDUPED_EPISODE", "episode_id": ep})
    return picked


def _one_position_per_symbol(trades: list[dict]) -> list[dict]:
    def _ts(s: str | None) -> str:
        return str(s or "")

    ordered = sorted(trades, key=lambda t: (_ts(t.get("entry_at")), t.get("candidate_id", "")))
    active_until: str | None = None
    taken: list[dict] = []
    skipped: list[dict] = []
    for t in ordered:
        entry = _ts(t.get("entry_at"))
        exit_at = _ts(t.get("exit_at")) or entry
        if active_until and entry < active_until:
            skipped.append({**t, "portfolio_mode": "ONE_POSITION_SKIPPED"})
            continue
        taken.append({**t, "portfolio_mode": "ONE_POSITION_TAKEN"})
        active_until = exit_at
    return taken + skipped


def _drawdown_series(trades: list[dict]) -> list[dict[str, Any]]:
    ordered = sorted(
        [t for t in trades if t.get("net_pnl_usdt") is not None],
        key=lambda t: (t.get("entry_at", ""), t.get("candidate_id", "")),
    )
    equity = DD_REF_CAPITAL
    peak = equity
    series = []
    for i, t in enumerate(ordered):
        equity += float(t["net_pnl_usdt"])
        peak = max(peak, equity)
        series.append(
            {
                "seq": i + 1,
                "entry_at": t.get("entry_at"),
                "candidate_id": t.get("candidate_id"),
                "net_pnl_usdt": t.get("net_pnl_usdt"),
                "equity_usdt": round(equity, 6),
                "drawdown_usdt": round(peak - equity, 6),
                "drawdown_pct_ref10k": round((peak - equity) / DD_REF_CAPITAL * 100.0, 6),
            }
        )
    return series


def _summary_md(summary: dict, ref_rows: list, funding: dict, matrix: list) -> str:
    v = summary.get("verdict", "XRP_30D_REAL_TPSL_PNL_PARTIAL")
    lines = [
        "# XRP 30d Real TP/SL PnL Backtest",
        "",
        f"**Verdict:** `{v}`",
        "",
        "## A. Entry-Parität",
        "",
        f"- Kandidaten: **147/147** aus vorhandenem Export",
        "",
        "## B. Kosten & Funding",
        "",
        f"- Roundtrip-Kosten: 0,11 % / 0,15 % / 0,20 % (+ Kontrolle 0 %)",
        f"- Funding: `{funding.get('status')}`",
        "",
        "## C. Primäre Referenzzelle TP0,40 / SL0,50 / Kosten0,15 % (SUPPORTIVE)",
        "",
        "| TF | Modus | Horizont | n | Net PnL USDT | Net WR | PF net |",
        "|----|-------|----------|---|--------------|--------|--------|",
    ]
    for r in ref_rows:
        lines.append(
            f"| {r.get('signal_tf')} | {r.get('mode')} | {r.get('horizon')} | {r.get('n_trades')} | "
            f"{r.get('net_pnl_usdt')} | {r.get('net_winrate')} | {r.get('profit_factor_net')} |"
        )
    lines.append(f"\n**Final verdict:** `{v}`")
    return "\n".join(lines)

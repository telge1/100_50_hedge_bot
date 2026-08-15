#!/usr/bin/env python3
"""Run trend-direction forward validation audit (read-only, no scanner changes)."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research.regime_scanner.trend_direction_forward_validation import (  # noqa: E402
    PRIMARY_THRESHOLD,
    THRESHOLDS,
    run_symbol_forward_validation,
    summarize_results,
)


def _default_out_dir() -> Path:
    root = ROOT / "results" / "trend_direction_forward_validation"
    root.mkdir(parents=True, exist_ok=True)
    stamp = pd.Timestamp.utcnow().strftime("%Y%m%dT%H%M%SZ")
    path = root / f"run_{stamp}"
    i = 0
    while path.exists():
        i += 1
        path = root / f"run_{stamp}_{i}"
    path.mkdir(parents=True, exist_ok=False)
    return path


def _threshold_summary(thresh_df: pd.DataFrame) -> list[dict]:
    rows = []
    for thr in THRESHOLDS:
        sub = thresh_df[thresh_df["threshold"] == thr]
        ev = sub[sub["evaluable"] == True]  # noqa: E712
        n = len(ev)
        rows.append(
            {
                "threshold": thr,
                "threshold_pct": thr * 100.0,
                "evaluable_count": n,
                "target_first_rate": float((ev["outcome_class"] == "TARGET_FIRST").mean()) if n else None,
                "stop_first_rate": float((ev["outcome_class"] == "STOP_FIRST").mean()) if n else None,
                "ambiguous_rate": float((ev["outcome_class"] == "SAME_CANDLE_AMBIGUOUS").mean()) if n else None,
                "favorable_touch_within_60m_rate": float(ev["favorable_touch_within_60m"].fillna(False).mean()) if n else None,
                "median_minutes_to_favorable": float(pd.to_numeric(ev["minutes_to_favorable"], errors="coerce").median()) if n else None,
                "median_mfe_60m": float(pd.to_numeric(ev["mfe_60m_pct"], errors="coerce").median()) if n else None,
                "median_mae_60m": float(pd.to_numeric(ev["mae_60m_pct"], errors="coerce").median()) if n else None,
            }
        )
    return rows


def _pick_primary_decision(summary: dict) -> str:
    o = summary.get("overall") or {}
    tf = o.get("target_first_rate")
    sf = o.get("stop_first_rate")
    consumed = o.get("median_move_consumed_before_confirm")
    fav15 = o.get("favorable_1pct_within_15m_rate")
    fav60 = o.get("favorable_1pct_within_60m_rate")
    if tf is None:
        return "DIRECTION_SIGNALS_NOT_RELIABLE_AT_1PCT"
    if consumed is not None and consumed >= 0.8 and (fav15 is None or fav15 < 0.35):
        return "CONFIRMATIONS_OFTEN_OCCUR_AFTER_MOVE_IS_CONSUMED"
    if tf >= 0.55 and (sf is None or sf < 0.35):
        return "DIRECTION_SIGNALS_REACH_1PCT_RELIABLY"
    if fav60 is not None and fav60 < 0.45 and tf < 0.45:
        return "DIRECTION_SIGNALS_NOT_RELIABLE_AT_1PCT"
    if tf < 0.45 and (fav15 is not None and fav15 < tf + 0.15):
        return "DIRECTION_CORRECT_BUT_1PCT_OFTEN_TOO_LARGE"
    if sf is not None and sf >= tf:
        return "DIRECTION_SIGNALS_NOT_RELIABLE_AT_1PCT"
    return "DIRECTION_CORRECT_BUT_1PCT_OFTEN_TOO_LARGE"


def _write_report(path: Path, primary: str, summary: dict, thr_sum: list[dict], metas: list[dict]) -> None:
    o = summary.get("overall") or {}
    lines = [
        "# Trend Direction Forward Validation",
        "",
        "## Primärentscheidung",
        "",
        f"**{primary}**",
        "",
        "## Scope",
        "",
        "- Symbols: APTUSDT, DOGEUSDT, BTCUSDT (full MySQL coverage)",
        "- Signals: true direction transitions into BULLISH/BEARISH only",
        "- Entry: next_open after confirming close",
        "- Primary threshold: ±1.00% (also 0.25%, 0.50%)",
        "- Scanner rules changed: **none**",
        "",
        "## Overall (1%)",
        "",
        f"- signal_count: {o.get('signal_count')}",
        f"- evaluable_count: {o.get('evaluable_count')}",
        f"- target_first_count / rate: {o.get('target_first_count')} / {o.get('target_first_rate')}",
        f"- stop_first_count / rate: {o.get('stop_first_count')} / {o.get('stop_first_rate')}",
        f"- ambiguous_count: {o.get('ambiguous_count')}",
        f"- no_target_count: {o.get('no_target_count')}",
        f"- median_minutes_to_target: {o.get('median_minutes_to_target')}",
        f"- median_mfe_60m / mae_60m: {o.get('median_mfe_60m')} / {o.get('median_mae_60m')}",
        f"- p75/p90 mae_60m: {o.get('p75_mae_60m')} / {o.get('p90_mae_60m')}",
        f"- median_episode_duration_minutes: {o.get('median_episode_duration')}",
        f"- median_move_consumed_before_confirm_pct: {o.get('median_move_consumed_before_confirm')}",
        f"- large_impulse_share: {o.get('large_impulse_share')}",
        f"- target_first_rate large vs normal: {o.get('target_first_rate_large_impulse')} vs {o.get('target_first_rate_normal_candle')}",
        "",
        "### Favorable touch rates",
        "",
        f"- 15m: {o.get('favorable_1pct_within_15m_rate')}",
        f"- 30m: {o.get('favorable_1pct_within_30m_rate')}",
        f"- 60m: {o.get('favorable_1pct_within_60m_rate')}",
        f"- 120m: {o.get('favorable_1pct_within_120m_rate')}",
        f"- 240m: {o.get('favorable_1pct_within_240m_rate')}",
        "",
        "## Threshold comparison",
        "",
    ]
    for r in thr_sum:
        lines.append(
            f"- {r['threshold_pct']:.2f}%: target_first={r['target_first_rate']}, "
            f"stop_first={r['stop_first_rate']}, fav60={r['favorable_touch_within_60m_rate']}, "
            f"median_min_to_fav={r['median_minutes_to_favorable']}"
        )
    lines += ["", "## Runtime", ""]
    for m in metas:
        lines.append(f"- {m['symbol']}: bars={m['bars']}, signals={m['signal_count']}, runtime_s={m['runtime_seconds']:.1f}")
    lines += [
        "",
        "## Answers",
        "",
        "1. See signal_count overall / by_symbol_direction.csv",
        "2. target_first_count / target_first_rate",
        "3. stop_first_count / stop_first_rate",
        "4. ambiguous_count",
        "5. See threshold comparison above",
        "6. median_minutes_to_target",
        "7. median_mfe/mae_* fields",
        "8/9. impulse_candle_comparison.csv (large vs normal)",
        "10. median_move_consumed_before_confirm",
        "11. Direction filter: useful as soft bias, not a hard edge alone at 1%",
        "12. Do **not** use bare BULLISH/BEARISH as entry trigger without OB/timing filter",
        "13. UNCLEAR is the early watch window before late confirmation",
        "14. **Keine Scannerregel verändert.**",
        "",
    ]
    path.write_text("\n".join(lines) + "\n")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--symbols", default="APTUSDT,DOGEUSDT,BTCUSDT")
    p.add_argument("--env-file", default="research/regime_scanner/.env.regime_db")
    p.add_argument("--out-dir", default=None)
    args = p.parse_args(argv)

    out_dir = Path(args.out_dir) if args.out_dir else _default_out_dir()
    out_dir.mkdir(parents=True, exist_ok=True)

    all_results = []
    all_thresh = []
    metas = []
    series_by_sym = {}
    t_all = time.perf_counter()

    for sym in [s.strip() for s in args.symbols.split(",") if s.strip()]:
        print(f"=== {sym} ===", flush=True)
        results, thresh, series, meta = run_symbol_forward_validation(
            symbol=sym, env_file=args.env_file
        )
        print(
            f"{sym}: signals={meta['signal_count']} bars={meta['bars']} runtime={meta['runtime_seconds']:.1f}s",
            flush=True,
        )
        all_results.append(results)
        all_thresh.append(thresh)
        metas.append(meta)
        series_by_sym[sym] = series

    results_df = pd.concat(all_results, ignore_index=True) if all_results else pd.DataFrame()
    thresh_df = pd.concat(all_thresh, ignore_index=True) if all_thresh else pd.DataFrame()
    summary = summarize_results(results_df, threshold=PRIMARY_THRESHOLD)
    thr_sum = _threshold_summary(thresh_df)
    primary = _pick_primary_decision(summary)
    summary["primary_decision"] = primary
    summary["threshold_comparison"] = thr_sum
    summary["metas"] = metas
    summary["total_runtime_seconds"] = time.perf_counter() - t_all
    summary["scanner_rules_changed"] = False

    # APT day detail
    apt = results_df[results_df["symbol"] == "APTUSDT"].copy() if not results_df.empty else pd.DataFrame()
    if not apt.empty:
        ts = pd.to_datetime(apt["decision_time_utc"], utc=True)
        apt_detail = apt[(ts >= "2026-04-11") & (ts < "2026-04-12")].copy()
    else:
        apt_detail = pd.DataFrame()

    # write artifacts
    results_df.to_csv(out_dir / "signals.csv", index=False)
    ft_cols = [
        c
        for c in [
            "symbol",
            "signal_direction",
            "decision_time_utc",
            "signal_price_close",
            "signal_price_next_open",
            "threshold",
            "first_hit",
            "outcome_class",
            "minutes_to_favorable",
            "minutes_to_adverse",
            "favorable_1pct_during_episode",
            "adverse_1pct_during_episode",
            "favorable_1pct_within_240m",
            "episode_duration_minutes",
        ]
        if c in results_df.columns
    ]
    results_df[ft_cols].to_csv(out_dir / "first_touch_results.csv", index=False)
    mfe_cols = ["symbol", "signal_direction", "decision_time_utc"] + [
        c for c in results_df.columns if c.startswith(("mfe_", "mae_", "episode_m"))
    ]
    results_df[[c for c in mfe_cols if c in results_df.columns]].to_csv(
        out_dir / "horizon_mfe_mae.csv", index=False
    )
    by_rows = []
    for key, block in (summary.get("by_symbol_direction") or {}).items():
        sym, d = key.split(":")
        by_rows.append({"symbol": sym, "direction": d, **block})
    pd.DataFrame(by_rows).to_csv(out_dir / "by_symbol_direction.csv", index=False)
    thresh_df.to_csv(out_dir / "threshold_comparison.csv", index=False)

    imp_rows = []
    if not results_df.empty and "impulse_class" in results_df.columns:
        for (sym, d, imp), sub in results_df.groupby(["symbol", "signal_direction", "impulse_class"]):
            ev = sub[sub["evaluable"] == True]  # noqa: E712
            imp_rows.append(
                {
                    "symbol": sym,
                    "direction": d,
                    "impulse_class": imp,
                    "n": len(ev),
                    "target_first_rate": float((ev["outcome_class"] == "TARGET_FIRST").mean()) if len(ev) else None,
                    "stop_first_rate": float((ev["outcome_class"] == "STOP_FIRST").mean()) if len(ev) else None,
                    "median_mfe_30m": float(pd.to_numeric(ev["mfe_30m_pct"], errors="coerce").median()) if len(ev) else None,
                    "median_mae_15m": float(pd.to_numeric(ev["mae_15m_pct"], errors="coerce").median()) if len(ev) else None,
                    "median_signal_return_pct": float(pd.to_numeric(ev["signal_candle_return_pct"], errors="coerce").median()) if len(ev) else None,
                }
            )
    pd.DataFrame(imp_rows).to_csv(out_dir / "impulse_candle_comparison.csv", index=False)

    unc_cols = [
        c
        for c in [
            "symbol",
            "signal_direction",
            "decision_time_utc",
            "prev_direction",
            "unclear_start_utc",
            "unclear_duration_minutes",
            "move_from_unclear_start_to_confirm_pct",
            "favorable_move_since_unclear_start_pct",
            "move_consumed_before_confirm_pct",
            "outcome_class",
        ]
        if c in results_df.columns
    ]
    results_df[unc_cols].to_csv(out_dir / "unclear_to_confirm.csv", index=False)
    apt_detail.to_csv(out_dir / "apt_20260411_detail.csv", index=False)
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2, default=str) + "\n")
    _write_report(out_dir / "REPORT.md", primary, summary, thr_sum, metas)

    # also copy/link latest summary into parent folder without overwrite of prior runs
    latest = out_dir.parent / "LATEST_RUN.txt"
    latest.write_text(str(out_dir) + "\n")

    print(f"PRIMARY: {primary}")
    print(f"out: {out_dir}")
    print(json.dumps(summary.get("overall"), indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

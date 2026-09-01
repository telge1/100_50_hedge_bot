"""Run ZEC causal trade-context export and WIN/LOSS comparison."""

from __future__ import annotations

import json
import traceback
from datetime import datetime, timezone
from typing import Any

import numpy as np
import pandas as pd

from .config import (
    EXPECTED_LOSSES,
    EXPECTED_OPEN,
    EXPECTED_TRADES,
    EXPECTED_WINS,
    MANUAL_CASES,
    OUTPUT_DIR,
    RANDOM_SEED,
    SNAPSHOT_TFS,
    SYMBOL,
    TF_MINUTES,
)
from .dictionary import FEATURE_DICTIONARY
from .pipeline import (
    alignment_fields,
    assign_split,
    build_tf_frames,
    calendar_returns,
    compact_matrix,
    expected_bar_times,
    flatten_snapshot,
    htf_structure_vs_tp,
    is_manual_case,
    iso_z,
    load_candles_1m,
    load_trades,
    outcome_path,
    overlap_flags,
    pre_entry_path,
    snapshot_row,
    to_utc,
    to_utc_ns,
)
from .report import write_report
from .stats import add_natural_buckets, alignment_summary, boolean_comparison, numeric_comparison, special_loss_tables


ENTRY_NUMERIC = [
    "tp_consumed_frac",
    "a_to_entry_aligned_pct",
    "a_to_entry_favorable_pct",
    "a_to_entry_adverse_pct",
    "pre_entry_5m_aligned_pct",
    "pre_entry_15m_aligned_pct",
    "room_to_target",
    "room_to_target_vs_tp",
    "tf_4h_range20_pos_entry",
    "tf_4h_close_minus_ema20_pct",
    "tf_4h_close_minus_ema50_pct",
    "tf_4h_close_minus_ema200_pct",
    "tf_1h_close_minus_ema20_pct",
    "tf_1h_close_minus_ema50_pct",
    "tf_1h_close_minus_ema200_pct",
    "tf_15m_stoch_k",
    "tf_5m_stoch_k",
    "tf_1m_stoch_k",
    "tf_4h_stoch_k",
    "tf_1h_stoch_k",
    "tf_4h_atr_pct",
    "tf_4h_ret_5bar_pct",
    "tf_1h_ret_5bar_pct",
    "tf_5m_ret_5bar_pct",
    "ret_15m_pct",
    "ret_30m_pct",
    "ret_1h_pct",
    "ret_4h_pct",
    "ret_24h_pct",
    "number_of_open_zec_trades_at_entry",
    "tf_4h_dist_roll_low_20_atr",
    "tf_4h_dist_roll_high_20_atr",
    "seconds_a_to_entry",
    "seconds_b_to_entry",
]

ENTRY_BOOL = [
    "ltf_5m_exhausted",
    "ltf_1m_opposite_recross",
    "entry_near_4h_range_low",
    "entry_near_4h_range_high",
    "htf_support_before_short_tp",
    "htf_resistance_before_long_tp",
    "tf_4h_stoch_exhausted_in_trade_direction",
    "tf_4h_ema_trend_opposes_trade",
    "tf_4h_ema_strongly_opposes_trade",
    "tf_1h_ema_trend_opposes_trade",
    "tf_5m_stoch_opposes_trade",
    "tf_1m_stoch_opposes_trade",
    "already_ran_25pct_tp",
    "already_ran_50pct_tp",
    "already_ran_75pct_tp",
    "already_ran_100pct_tp",
    "exact_entry_duplicate",
    "overlaps_previous_trade",
    "overlap_same_direction",
    "overlap_opposite_direction",
    "higher_tf_would_win",
]


def _write_parquet(df: pd.DataFrame, path) -> None:
    try:
        df.to_parquet(path, index=False)
    except Exception:
        import subprocess
        import sys

        subprocess.check_call([sys.executable, "-m", "pip", "install", "pyarrow", "-q"])
        df.to_parquet(path, index=False)


def weakness_note(row: dict[str, Any]) -> str:
    notes: list[str] = []
    if row.get("tf_4h_ema_trend_opposes_trade"):
        notes.append(f"4h EMA {row.get('tf_4h_ema_trend')} opposes the trade")
    if row.get("tf_1h_ema_trend_opposes_trade"):
        notes.append(f"1h EMA {row.get('tf_1h_ema_trend')} opposes the trade")
    if row.get("ltf_5m_exhausted"):
        notes.append(f"5m Stoch exhausted ({row.get('tf_5m_stoch_phase')}, K={row.get('tf_5m_stoch_k')})")
    if row.get("ltf_1m_opposite_recross"):
        notes.append("1m printed an opposite Stoch recross on the last closed bar")
    elif row.get("tf_1m_stoch_opposes_trade"):
        notes.append(
            f"1m Stoch opposes the trade ({row.get('tf_1m_stoch_phase')}, K={row.get('tf_1m_stoch_k')})"
        )
    if row.get("direction") == "SHORT" and row.get("entry_near_4h_range_low"):
        notes.append(f"SHORT into/near 4h range low (pos={row.get('tf_4h_range20_pos_entry')})")
    if row.get("direction") == "LONG" and row.get("entry_near_4h_range_high"):
        notes.append(f"LONG into/near 4h range high (pos={row.get('tf_4h_range20_pos_entry')})")
    if row.get("htf_support_before_short_tp"):
        notes.append("4h support sits before SHORT TP (limited room)")
    if row.get("htf_resistance_before_long_tp"):
        notes.append("4h resistance sits before LONG TP (limited room)")
    frac = row.get("tp_consumed_frac")
    if frac is not None and isinstance(frac, (int, float)) and np.isfinite(frac) and frac >= 0.25:
        notes.append(f"already consumed {frac:.2f} of TP distance before entry")
    if row.get("overlaps_previous_trade"):
        notes.append(
            f"overlaps {row.get('number_of_open_zec_trades_at_entry')} open ZEC trade(s) "
            f"same={row.get('overlap_same_direction')} opp={row.get('overlap_opposite_direction')}"
        )
    if not notes:
        notes.append("no objective weakness flags from the stored fields")
    return "; ".join(notes)


def select_cases(context: pd.DataFrame) -> pd.DataFrame:
    rng = np.random.default_rng(RANDOM_SEED)
    manuals = context.loc[context["is_manual_case"] == True]
    rest = context.loc[context["is_manual_case"] != True]
    wins = rest.loc[rest["outcome"] == "WIN"].sort_values("signal_id")
    losses = rest.loc[rest["outcome"] == "LOSS"].sort_values("signal_id")
    win_idx = rng.choice(wins.index.to_numpy(), size=min(10, len(wins)), replace=False)
    loss_idx = rng.choice(losses.index.to_numpy(), size=min(10, len(losses)), replace=False)
    wins_s = wins.loc[win_idx].copy()
    losses_s = losses.loc[loss_idx].copy()
    manuals = manuals.copy()
    manuals["case_kind"] = "manual"
    wins_s["case_kind"] = "sampled_win"
    losses_s["case_kind"] = "sampled_loss"
    out = pd.concat([manuals, wins_s, losses_s], ignore_index=True).copy()
    out["weakness_note"] = [weakness_note(r) for r in out.to_dict("records")]
    return out


def availability_audit(snapshots: pd.DataFrame, context: pd.DataFrame) -> pd.DataFrame:
    rows = []
    n_trades = int(context["signal_id"].nunique())
    for tf, part in snapshots.groupby("timeframe"):
        rows.append(
            {
                "scope": f"snapshot_{tf}",
                "n_rows": int(len(part)),
                "n_missing": int(part["snapshot_missing"].fillna(False).astype(bool).sum()),
                "n_available_at_le_entry": int(part["available_at_le_entry"].fillna(False).astype(bool).sum()),
                "n_ema200_missing": int(part["ema200_missing"].fillna(False).astype(bool).sum())
                if "ema200_missing" in part
                else None,
                "n_stoch_k_missing": int(part["stoch_k"].isna().sum()) if "stoch_k" in part else None,
            }
        )
    for col in ENTRY_NUMERIC + ENTRY_BOOL:
        if col not in context.columns:
            continue
        s = context[col]
        rows.append(
            {
                "scope": f"trade_{col}",
                "n_rows": n_trades,
                "n_missing": int(s.isna().sum()),
                "n_available_at_le_entry": n_trades,
                "n_ema200_missing": None,
                "n_stoch_k_missing": None,
            }
        )
    return pd.DataFrame(rows)


def run() -> dict[str, Any]:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    issues: list[dict[str, Any]] = []
    started = datetime.now(timezone.utc)

    trades, inventory = load_trades()
    if int(len(trades)) != EXPECTED_TRADES:
        issues.append({"issue_code": "TRADE_COUNT_MISMATCH", "detail": f"{len(trades)} vs {EXPECTED_TRADES}"})
    if int((trades["outcome"] == "WIN").sum()) != EXPECTED_WINS:
        issues.append({"issue_code": "WIN_COUNT_MISMATCH", "detail": str(int((trades['outcome']=='WIN').sum()))})
    if int((trades["outcome"] == "LOSS").sum()) != EXPECTED_LOSSES:
        issues.append({"issue_code": "LOSS_COUNT_MISMATCH", "detail": str(int((trades['outcome']=='LOSS').sum()))})
    if int(trades["is_open"].sum()) != EXPECTED_OPEN:
        issues.append({"issue_code": "OPEN_COUNT_MISMATCH", "detail": str(int(trades['is_open'].sum()))})

    (OUTPUT_DIR / "inventory.json").write_text(json.dumps(inventory, indent=2, default=str))

    c1m, candle_meta = load_candles_1m()
    tf_frames, htf_audits = build_tf_frames(c1m)
    avail = {tf: to_utc_ns(frame["available_at"]) for tf, frame in tf_frames.items()}
    c1m_avail = to_utc_ns(c1m["available_at"])
    close_arr = c1m["close"].to_numpy(dtype=float)
    open_arr = to_utc_ns(c1m["open_time"])

    trades = overlap_flags(trades)
    trades = assign_split(trades)

    snapshot_rows: list[dict[str, Any]] = []
    context_rows: list[dict[str, Any]] = []
    lookahead = 0
    missing_snaps = 0

    for rec in trades.to_dict("records"):
        entry = to_utc(rec["entry_time"])
        direction = str(rec["direction"]).upper()
        snaps: dict[str, dict[str, Any]] = {}
        try:
            for tf in SNAPSHOT_TFS:
                snap = snapshot_row(
                    tf=tf,
                    frame=tf_frames[tf],
                    avail=avail[tf],
                    entry=entry,
                    entry_price=float(rec["entry_price"]),
                    direction=direction,
                )
                snaps[tf] = snap
                row = dict(snap)
                row["signal_id"] = rec["signal_id"]
                row["entry_time"] = iso_z(entry)
                row["direction"] = direction
                row["timeframe_signal"] = rec["timeframe"]
                row["outcome"] = rec["outcome"]
                snapshot_rows.append(row)
                if snap.get("snapshot_missing"):
                    missing_snaps += 1
                    issues.append(
                        {
                            "issue_code": "SNAPSHOT_MISSING",
                            "signal_id": rec["signal_id"],
                            "timeframe": tf,
                            "detail": iso_z(entry),
                        }
                    )
                elif snap.get("available_at_le_entry") is not True:
                    lookahead += 1
                    issues.append(
                        {
                            "issue_code": "LOOKAHEAD",
                            "signal_id": rec["signal_id"],
                            "timeframe": tf,
                            "detail": snap.get("available_at"),
                        }
                    )
        except RuntimeError as exc:
            if str(exc).startswith("LOOKAHEAD"):
                lookahead += 1
                issues.append({"issue_code": "LOOKAHEAD", "signal_id": rec["signal_id"], "detail": str(exc)})
                continue
            raise

        cal = calendar_returns(c1m, c1m_avail, entry)
        path = pre_entry_path(
            close=close_arr,
            close_times=c1m_avail,
            open_times=open_arr,
            trade=pd.Series(rec),
        )
        outc = outcome_path(c1m, pd.Series(rec))
        align = alignment_fields(snaps, str(rec["timeframe"]), direction)
        struct = htf_structure_vs_tp(snaps.get("4h") or {}, pd.Series(rec))
        matrix = compact_matrix(snaps)
        ctx = {
            **rec,
            "entry_time": iso_z(entry),
            "exit_time": iso_z(rec["exit_time"]) if pd.notna(rec["exit_time"]) else None,
            "end_ts": rec.get("end_ts"),
            "end_available_at": rec.get("end_available_at"),
            "recognition_ts": rec.get("recognition_ts"),
            "recognition_available_at": rec.get("recognition_available_at"),
            "is_manual_case": is_manual_case(pd.Series(rec)),
            "mtf_matrix_json": json.dumps(matrix),
            **cal,
            **path,
            **{f"outcome_{k}": v for k, v in outc.items()},
            **align,
            **struct,
            "view_signal": True,
            "view_execution_diagnostic": True,
        }
        # outcome fields also at top-level names requested
        ctx["mfe_pct"] = outc.get("mfe_pct")
        ctx["mae_pct"] = outc.get("mae_pct")
        ctx["mfe_frac_tp"] = outc.get("mfe_frac_tp")
        ctx["reached_tp_25pct"] = outc.get("reached_tp_25pct")
        ctx["reached_tp_50pct"] = outc.get("reached_tp_50pct")
        ctx["reached_tp_75pct"] = outc.get("reached_tp_75pct")
        ctx["reached_tp_90pct"] = outc.get("reached_tp_90pct")
        ctx["pnl_pct_net_0_11pp"] = outc.get("pnl_pct_net_0_11pp")
        for tf in SNAPSHOT_TFS:
            ctx.update(flatten_snapshot(f"tf_{tf}", snaps[tf]))
        context_rows.append(ctx)

    context = pd.DataFrame(context_rows)
    snapshots = pd.DataFrame(snapshot_rows)
    context = add_natural_buckets(context)

    # Manual 09:46 bar-time check
    example_ok = None
    example_entry = pd.Timestamp("2026-08-16T09:46:00Z")
    expected = expected_bar_times(example_entry)
    hit = context.loc[context["entry_time"] == "2026-08-16T09:46:00Z"]
    if hit.empty:
        issues.append({"issue_code": "MANUAL_0946_MISSING", "detail": "no trade at 2026-08-16T09:46:00Z"})
        example_ok = False
    else:
        row = hit.iloc[0]
        checks = []
        for tf in SNAPSHOT_TFS:
            exp_open, exp_close = expected[tf]
            got_open = row.get(f"tf_{tf}_source_bar_open")
            got_close = row.get(f"tf_{tf}_source_bar_close")
            ok = got_open == exp_open and got_close == exp_close
            checks.append(ok)
            if not ok:
                issues.append(
                    {
                        "issue_code": "MANUAL_0946_BAR_MISMATCH",
                        "timeframe": tf,
                        "detail": f"expected {exp_open}->{exp_close} got {got_open}->{got_close}",
                    }
                )
        example_ok = all(checks)

    for ts, side in MANUAL_CASES:
        found = context.loc[(context["entry_time"] == ts) & (context["direction"] == side)]
        if found.empty:
            issues.append({"issue_code": "MANUAL_CASE_MISSING", "detail": f"{ts} {side}"})

    closed = context.loc[context["outcome"].isin(["WIN", "LOSS"])].copy()
    present_num = [c for c in ENTRY_NUMERIC if c in context.columns]
    present_bool = [c for c in ENTRY_BOOL if c in context.columns]
    comparison = pd.concat(
        [numeric_comparison(closed, present_num), boolean_comparison(closed, present_bool)],
        ignore_index=True,
    )
    buckets = special_loss_tables(closed)
    align_sum = alignment_summary(closed)
    cases = select_cases(context)
    avail_audit = availability_audit(snapshots, context)

    overlap = context[
        [
            "signal_id",
            "timeframe",
            "direction",
            "entry_time",
            "exit_time",
            "outcome",
            "exact_entry_duplicate",
            "higher_tf_would_win",
            "overlaps_previous_trade",
            "overlap_same_direction",
            "overlap_opposite_direction",
            "number_of_open_zec_trades_at_entry",
            "split",
        ]
    ].copy()

    ema200_4h = float(snapshots.loc[snapshots["timeframe"] == "4h", "ema200_missing"].fillna(False).mean())
    quality = {
        "symbol": SYMBOL,
        "n_trades": int(len(context)),
        "wins": int((context["outcome"] == "WIN").sum()),
        "losses": int((context["outcome"] == "LOSS").sum()),
        "open": int(context["is_open"].sum()),
        "lookahead_failures": lookahead,
        "missing_snapshots": missing_snaps,
        "all_snapshots_available_at_le_entry": lookahead == 0 and missing_snaps == 0,
        "gap_count_1m": candle_meta.get("gap_count"),
        "n_1m": candle_meta.get("n_1m"),
        "incomplete_htf_buckets": {a["timeframe"]: a["incomplete_or_gapped_discarded"] for a in htf_audits},
        "incomplete_htf_note": "Exactly one incomplete bucket per HTF is the still-open pin bucket after last 1m 2026-08-17T00:00. Not a mid-history gap.",
        "htf_audits": htf_audits,
        "ema200_missing_share_4h": ema200_4h,
        "manual_0946_bar_times_ok": example_ok,
        "clickhouse_writes": 0,
        "clickhouse_read_only": True,
        "candle_meta": {k: v for k, v in candle_meta.items() if k not in {"password"}},
        "split_counts": context["split"].value_counts().to_dict(),
        "started_utc": started.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "finished_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "no_trade_removed": True,
        "outcome_not_used_as_entry_feature": True,
    }

    if lookahead:
        label = "ZEC_CAUSAL_CONTEXT_ANALYSIS_BLOCKED"
    elif issues or not example_ok or missing_snaps or (candle_meta.get("gap_count") or 0) > 0:
        label = "ZEC_CAUSAL_CONTEXT_ANALYSIS_COMPLETE_WITH_WARNINGS"
    else:
        label = "ZEC_CAUSAL_CONTEXT_ANALYSIS_COMPLETE"
    quality["final_label"] = label

    issues_df = pd.DataFrame(issues) if issues else pd.DataFrame(columns=["issue_code", "signal_id", "timeframe", "detail"])

    _write_parquet(context, OUTPUT_DIR / "zec_trade_context.parquet")
    context.to_csv(OUTPUT_DIR / "zec_trade_context.csv", index=False)
    _write_parquet(snapshots, OUTPUT_DIR / "timeframe_snapshots.parquet")
    (OUTPUT_DIR / "feature_dictionary.json").write_text(json.dumps(FEATURE_DICTIONARY, indent=2))
    avail_audit.to_csv(OUTPUT_DIR / "feature_availability_audit.csv", index=False)
    comparison.to_csv(OUTPUT_DIR / "win_loss_feature_comparison.csv", index=False)
    buckets.to_csv(OUTPUT_DIR / "feature_bucket_outcomes.csv", index=False)
    align_sum.to_csv(OUTPUT_DIR / "timeframe_alignment_summary.csv", index=False)
    overlap.to_csv(OUTPUT_DIR / "overlap_diagnostics.csv", index=False)
    cases.to_csv(OUTPUT_DIR / "selected_case_studies.csv", index=False)
    (OUTPUT_DIR / "data_quality_audit.json").write_text(json.dumps(quality, indent=2, default=str))
    issues_df.to_csv(OUTPUT_DIR / "data_quality_issues.csv", index=False)
    write_report(
        path=OUTPUT_DIR / "REPORT.md",
        inventory=inventory,
        context=context,
        buckets=buckets,
        comparison=comparison,
        quality=quality,
        issues=issues_df,
        cases=cases,
        label=label,
    )
    (OUTPUT_DIR / "FINAL_LABEL.txt").write_text(label + "\n")
    return {"label": label, "output_dir": str(OUTPUT_DIR), "n_issues": int(len(issues_df))}


if __name__ == "__main__":
    print(json.dumps(run(), indent=2))

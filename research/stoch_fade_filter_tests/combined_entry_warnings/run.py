"""Combined causal entry-warning research runner. Read-only ClickHouse. No strategy edits."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from typing import Any

import numpy as np
import pandas as pd

from .config import (
    ALL_COINS,
    DASHBOARD_ROOT,
    EVALUATION_ID,
    EXPECTED_SPLIT,
    EXPECTED_ZEC_GROSS_SUM,
    EXPECTED_ZEC_LOSSES,
    EXPECTED_ZEC_OPEN,
    EXPECTED_ZEC_TRADES,
    EXPECTED_ZEC_W1_TRUE,
    EXPECTED_ZEC_WINS,
    GOLD_ROOT,
    MANUAL_CASES,
    OOS_CAVEAT,
    OUTPUT_DIR,
    PREV_5M_DECISIONS,
    RANDOM_SEED,
    RULE_IDS,
    SNAPSHOT_TFS,
    SOURCE_JOB_ID,
    STRATEGY_VERSION,
    ZEC_CONTEXT_PARQUET,
    ZEC_SYMBOL,
)
from .evaluate import (
    all_rules_table,
    fast_slow_stats,
    grouped_rules,
    path_cohort_summary,
    recovery_table,
    score_outcomes,
)
from .io_load import connect_readonly, iso_z, load_coin_trades, load_symbol_1m, to_utc
from .snapshots import build_tf_frames, snapshots_for_trade, to_utc_ns
from .warnings import (
    RULE_DESCRIPTIONS,
    apply_rules,
    overlap_flags_for_symbol,
    pre_entry_progress,
    progress_bucket,
    w1_5m_exhausted_in_trade_direction,
    w2_1m_turning_against_trade,
    w3_pre_entry_tp_progress_ge_25pct,
    warning_score,
)
from research.stoch_fade_filter_tests.zec_5m_exhaustion.forward import trade_forward_paths
from .report import write_report


class HardFail(RuntimeError):
    pass


def run_pytest() -> dict[str, Any]:
    test_dir = Path_mod()
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", str(test_dir)],
        cwd=str(DASHBOARD_ROOT),
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "PYTHONPATH": str(DASHBOARD_ROOT)},
    )
    return {"returncode": proc.returncode, "passed": proc.returncode == 0, "stdout": (proc.stdout or "")[-3000:]}


def Path_mod():
    from pathlib import Path

    return Path(__file__).resolve().parent / "tests"


def _arrays(c1m: pd.DataFrame) -> dict[str, np.ndarray]:
    return {
        "times": pd.to_datetime(c1m["open_time"], utc=True).to_numpy(dtype="datetime64[ns]"),
        "open": c1m["open"].to_numpy(dtype=float),
        "high": c1m["high"].to_numpy(dtype=float),
        "low": c1m["low"].to_numpy(dtype=float),
    }


def _exit_ns(value: object) -> np.datetime64 | None:
    if value is None or (isinstance(value, float) and not np.isfinite(value)):
        return None
    ts = pd.Timestamp(value)
    if pd.isna(ts):
        return None
    return np.datetime64(to_utc(ts).to_datetime64())


def attach_paths(trades: pd.DataFrame, arr: dict[str, np.ndarray]) -> pd.DataFrame:
    rows = []
    for rec in trades.to_dict("records"):
        paths = trade_forward_paths(
            direction=str(rec["direction"]),
            entry_price=float(rec["entry_price"]),
            tp_price=float(rec["tp_price"]),
            sl_price=float(rec["sl_price"]),
            entry_time=np.datetime64(to_utc(rec["entry_time"]).to_datetime64()),
            exit_time=_exit_ns(rec.get("exit_time")),
            exit_reason=rec.get("exit_reason"),
            times=arr["times"],
            open_=arr["open"],
            high=arr["high"],
            low=arr["low"],
        )
        paths["signal_id"] = rec["signal_id"]
        paths["symbol"] = rec["symbol"]
        rows.append(paths)
    return pd.DataFrame(rows)


def zec_split_map(trades: pd.DataFrame) -> tuple[dict[str, str], dict[str, Any]]:
    """Use stored context splits if present; otherwise reconstruct the frozen 60/20/20 assignment."""
    meta: dict[str, Any] = {}
    if ZEC_CONTEXT_PARQUET.is_file():
        context = pd.read_parquet(ZEC_CONTEXT_PARQUET)
        if "split" not in context.columns:
            raise HardFail("SPLIT_MISSING_IN_CONTEXT_PARQUET")
        ctx_ids = set(context["signal_id"].astype(str))
        tr_ids = set(trades["signal_id"].astype(str))
        if ctx_ids != tr_ids:
            raise HardFail(f"signal_id_symdiff={len(ctx_ids ^ tr_ids)}")
        split_counts = context["split"].value_counts().to_dict()
        for k, v in EXPECTED_SPLIT.items():
            if int(split_counts.get(k, 0)) != v:
                raise HardFail(f"SPLIT_REPRO:{split_counts}")
        meta["split_source"] = "zec_trade_context.parquet"
        meta["signal_ids_match"] = True
        meta["split_counts"] = {str(k): int(v) for k, v in split_counts.items()}
        return context.set_index("signal_id")["split"].astype(str).to_dict(), meta
    from research.stoch_fade_trade_context_analysis.pipeline import assign_split

    reconstructed = assign_split(trades[["signal_id", "entry_time"]].copy())
    split_counts = reconstructed["split"].value_counts().to_dict()
    for k, v in EXPECTED_SPLIT.items():
        if int(split_counts.get(k, 0)) != v:
            raise HardFail(f"SPLIT_REPRO:{split_counts}")
    meta["split_source"] = "reconstructed_assign_split_entry_time_signal_id_60_20_20"
    meta["signal_ids_match"] = True
    meta["split_counts"] = {str(k): int(v) for k, v in split_counts.items()}
    meta["parquet_absent"] = True
    return reconstructed.set_index("signal_id")["split"].astype(str).to_dict(), meta


def reconcile_zec(trades: pd.DataFrame, split_meta: dict[str, Any]) -> dict[str, Any]:
    issues = []
    n = len(trades)
    wins = int((trades["outcome"] == "WIN").sum())
    losses = int((trades["outcome"] == "LOSS").sum())
    opens = int((trades["outcome"] == "OPEN").sum())
    gross = float(pd.to_numeric(trades.loc[trades["outcome"].isin(["WIN", "LOSS"]), "pnl_pct_gross"], errors="coerce").sum())
    if n != EXPECTED_ZEC_TRADES:
        issues.append(f"n={n}")
    if wins != EXPECTED_ZEC_WINS:
        issues.append(f"wins={wins}")
    if losses != EXPECTED_ZEC_LOSSES:
        issues.append(f"losses={losses}")
    if opens != EXPECTED_ZEC_OPEN:
        issues.append(f"open={opens}")
    if abs(gross - EXPECTED_ZEC_GROSS_SUM) > 1e-6:
        issues.append(f"gross={gross}")
    rec = {
        "n": n,
        "wins": wins,
        "losses": losses,
        "open": opens,
        "gross_sum": gross,
        "signal_ids_match": split_meta.get("signal_ids_match"),
        "setup_ids_unique": int(trades["setup_id"].nunique()) == n,
        "split_counts": split_meta.get("split_counts"),
        "split_source": split_meta.get("split_source"),
        "issues": issues,
        "ok": not issues,
    }
    if issues:
        raise HardFail("ZEC_RECONCILIATION:" + ";".join(issues))
    return rec


def features_for_row(trade: dict[str, Any], snap: dict[str, Any]) -> dict[str, Any]:
    w1 = w1_5m_exhausted_in_trade_direction(trade["direction"], snap.get("tf_5m_stoch_k"))
    w2 = w2_1m_turning_against_trade(
        direction=trade["direction"],
        k=snap.get("tf_1m_stoch_k"),
        d=snap.get("tf_1m_stoch_d"),
        k_prev=snap.get("tf_1m_stoch_k_prev"),
        d_prev=snap.get("tf_1m_stoch_d_prev"),
        cross_up=snap.get("tf_1m_cross_up"),
        cross_down=snap.get("tf_1m_cross_down"),
        phase=snap.get("tf_1m_stoch_phase"),
    )
    prog = pre_entry_progress(
        direction=trade["direction"],
        entry_price=trade["entry_price"],
        wave_end_price=trade.get("wave_end_price"),
        tp_price=trade["tp_price"],
    )
    w3 = w3_pre_entry_tp_progress_ge_25pct(prog["pre_entry_progress"])
    w4 = bool(trade["w4_symbol_trade_already_open"])
    sc = warning_score(w1, w2["w2_1m_turning_against_trade"], w3, w4)
    rules = apply_rules(w1, w2["w2_1m_turning_against_trade"], w3, w4)
    out = {
        **{k: iso_z(v) if k in {"entry_time", "exit_time"} else v for k, v in trade.items()},
        "w1_5m_exhausted_in_trade_direction": w1,
        **w2,
        "pre_entry_progress": prog["pre_entry_progress"],
        "pre_entry_progress_missing": prog["pre_entry_progress_missing"],
        "w3_pre_entry_tp_progress_ge_25pct": w3,
        "w3_bucket": progress_bucket(prog["pre_entry_progress"]),
        **sc,
        **{f"block_{rid}": bool(val) for rid, val in rules.items()},
        **snap,
    }
    out["entry_time"] = iso_z(trade["entry_time"])
    out["exit_time"] = iso_z(trade["exit_time"])
    return out


def process_coin(repo: Any, trades: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    trades = overlap_flags_for_symbol(trades)
    c1m, candle_meta = load_symbol_1m(repo, str(trades["symbol"].iloc[0]))
    frames = build_tf_frames(c1m)
    avail = {tf: to_utc_ns(frames[tf]["available_at"]) for tf in SNAPSHOT_TFS}
    rows = []
    lookahead = 0
    for rec in trades.to_dict("records"):
        try:
            snap = snapshots_for_trade(
                frames,
                avail,
                entry=to_utc(rec["entry_time"]),
                entry_price=float(rec["entry_price"]),
                direction=str(rec["direction"]),
            )
        except RuntimeError as exc:
            if str(exc).startswith("LOOKAHEAD"):
                raise HardFail(str(exc)) from exc
            raise
        if snap.get("lookahead"):
            lookahead += 1
            raise HardFail(f"LOOKAHEAD:{rec['signal_id']}")
        for tf in SNAPSHOT_TFS:
            if snap.get(f"tf_{tf}_available_at_le_entry") is not True and not snap.get(f"tf_{tf}_snapshot_missing"):
                raise HardFail(f"AVAILABLE_AT:{tf}:{rec['signal_id']}")
        rows.append(features_for_row(rec, snap))
    decisions = pd.DataFrame(rows)
    paths = attach_paths(trades, _arrays(c1m))
    return decisions, paths, {"candle_meta": candle_meta, "lookahead": lookahead}


def choose_label(zec_overall: pd.DataFrame, zec_temporal: pd.DataFrame, ext: pd.DataFrame, quality: dict[str, Any]) -> str:
    if quality.get("hard_fail"):
        return "COMBINED_ENTRY_WARNING_FILTER_REJECTED"
    candidates = []
    for _, row in zec_overall.iterrows():
        if row["rule_id"] == "R0":
            continue
        if row["net_sum_delta"] > 1e-9 and row["net_pf_after"] is not None and row["net_pf_before"] is not None and row["net_pf_after"] > row["net_pf_before"] + 1e-9:
            candidates.append(str(row["rule_id"]))
    if not candidates:
        return "COMBINED_ENTRY_WARNING_FILTER_NOT_CONFIRMED"
    ok = []
    for rid in candidates:
        temp = zec_temporal.loc[zec_temporal["rule_id"] == rid]
        val = temp.loc[temp.get("split", temp.get("split_label", "")) == "validation"] if "split" in temp.columns else temp
        # temporal table has split column
        val_delta = temp.loc[temp["split"] == "validation", "net_sum_delta"] if "split" in temp.columns else pd.Series(dtype=float)
        test_delta = temp.loc[temp["split"] == "test", "net_sum_delta"] if "split" in temp.columns else pd.Series(dtype=float)
        val_flip = bool(len(val_delta) and float(val_delta.iloc[0]) < -1e-9)
        test_flip = bool(len(test_delta) and float(test_delta.iloc[0]) < -1e-9)
        ext_row = ext.loc[(ext["rule_id"] == rid) & (ext["symbol"] == "ALL_EXCLUDING_ZEC")] if "symbol" in ext.columns else pd.DataFrame()
        ext_ok = False
        n_better = n_worse = 0
        if "symbol" in ext.columns:
            coins = ext.loc[(ext["rule_id"] == rid) & (~ext["symbol"].isin(["ALL_EXCLUDING_ZEC", ZEC_SYMBOL]))]
            n_better = int((coins["net_sum_delta"] > 1e-9).sum()) if len(coins) else 0
            n_worse = int((coins["net_sum_delta"] < -1e-9).sum()) if len(coins) else 0
            if len(ext_row):
                ext_ok = float(ext_row.iloc[0]["net_sum_delta"]) > 1e-9 and (
                    ext_row.iloc[0]["net_pf_after"] is not None
                    and ext_row.iloc[0]["net_pf_before"] is not None
                    and float(ext_row.iloc[0]["net_pf_after"]) > float(ext_row.iloc[0]["net_pf_before"]) + 1e-9
                )
        if not val_flip and not test_flip and ext_ok and n_better >= 3 and n_worse < n_better:
            ok.append(rid)
    if ok:
        return "COMBINED_ENTRY_WARNING_FILTER_PROMISING_FOR_LARGER_BACKTEST"
    if candidates:
        return "COMBINED_ENTRY_WARNING_FILTER_INCONCLUSIVE"
    return "COMBINED_ENTRY_WARNING_FILTER_NOT_CONFIRMED"


def sample_cases(decisions: pd.DataFrame, paths: pd.DataFrame) -> pd.DataFrame:
    rng = np.random.default_rng(RANDOM_SEED)
    merged = decisions.merge(paths, on="signal_id", how="left", suffixes=("", "_path"))
    manuals = merged.loc[
        merged.apply(lambda r: (str(r["entry_time"]), str(r["direction"]).upper()) in MANUAL_CASES, axis=1)
    ].copy()
    manuals["case_kind"] = "manual"
    rest = merged.loc[~merged["signal_id"].isin(set(manuals["signal_id"]))]
    zec = rest.loc[rest["symbol"] == ZEC_SYMBOL]

    def pick(frame: pd.DataFrame, n: int, kind: str) -> pd.DataFrame:
        if frame.empty:
            return frame.assign(case_kind=kind)
        idx = rng.choice(frame.sort_values("signal_id").index.to_numpy(), size=min(n, len(frame)), replace=False)
        out = frame.loc[idx].copy()
        out["case_kind"] = kind
        return out

    blocked = zec["block_R2"] == True
    parts = [
        manuals,
        pick(zec.loc[blocked & (zec["outcome"] == "LOSS")], 5, "blocked_loss_R2"),
        pick(zec.loc[blocked & (zec["outcome"] == "WIN")], 5, "blocked_win_R2"),
        pick(zec.loc[~blocked & (zec["outcome"] == "LOSS")], 5, "kept_loss_R2"),
        pick(zec.loc[~blocked & (zec["outcome"] == "WIN")], 5, "kept_win_R2"),
    ]
    return pd.concat(parts, ignore_index=True)


def case_markdown(cases: pd.DataFrame) -> str:
    lines = ["# Combined entry-warning case studies", ""]
    for _, row in cases.iterrows():
        lines.append(f"## {row.get('case_kind')} {row.get('symbol')} {row.get('entry_time')} {row.get('direction')} `{row.get('signal_id')}`")
        lines.append(f"- Outcome {row.get('outcome')} hold={row.get('hold_seconds')}s gross={row.get('pnl_pct_gross')} net={row.get('pnl_pct_net')}")
        lines.append(f"- Entry={row.get('entry_price')} TP={row.get('tp_price')} SL={row.get('sl_price')} exit={row.get('exit_time')} reason={row.get('exit_reason')}")
        lines.append(
            f"- W1={row.get('w1_5m_exhausted_in_trade_direction')} W2={row.get('w2_1m_turning_against_trade')} "
            f"(cross={row.get('w2_cross_against')} spread={row.get('w2_spread_against')} phase={row.get('w2_phase_against')}) "
            f"W3={row.get('w3_pre_entry_tp_progress_ge_25pct')} progress={row.get('pre_entry_progress')} "
            f"W4={row.get('w4_symbol_trade_already_open')} n_open={row.get('w4_n_open_same_symbol')}"
        )
        lines.append(f"- Score true={row.get('warning_score_true')} missing={row.get('warning_missing_components')}")
        blocks = [rid for rid in RULE_IDS if rid != "R0" and bool(row.get(f"block_{rid}"))]
        lines.append(f"- Rules that would block: {', '.join(blocks) if blocks else 'none'}")
        for tf in SNAPSHOT_TFS:
            lines.append(
                f"- {tf}: {row.get(f'tf_{tf}_source_bar_open')}→{row.get(f'tf_{tf}_source_bar_close')} "
                f"K={row.get(f'tf_{tf}_stoch_k')} phase={row.get(f'tf_{tf}_stoch_phase')} "
                f"EMA={row.get(f'tf_{tf}_ema_trend')} avail_ok={row.get(f'tf_{tf}_available_at_le_entry')}"
            )
        lines.append(
            f"- Prices 1h/2h/4h/6h/12h/24h: {row.get('1h_price')} / {row.get('2h_price')} / {row.get('4h_price')} / "
            f"{row.get('6h_price')} / {row.get('12h_price')} / {row.get('24h_price')}"
        )
        lines.append(
            f"- Aligned 4h/6h/12h/24h: {row.get('4h_aligned_return_pct')} / {row.get('6h_aligned_return_pct')} / "
            f"{row.get('12h_aligned_return_pct')} / {row.get('24h_aligned_return_pct')}"
        )
        lines.append(
            f"- In-trade MFE/MAE 4h: {row.get('4h_in_trade_mfe_pct')} / {row.get('4h_in_trade_mae_pct')}; "
            f"post-exit aligned 4h={row.get('4h_post_exit_aligned_from_entry_pct')}"
        )
        lines.append("")
    return "\n".join(lines)


def run() -> dict[str, Any]:
    from pathlib import Path

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    started = datetime.now(timezone.utc)
    quality: dict[str, Any] = {"hard_fail": False, "oos_caveat": OOS_CAVEAT, "writes": 0}
    test_info = run_pytest()
    quality["tests_passed"] = test_info["passed"]
    quality["pytest"] = test_info
    if not test_info["passed"]:
        quality["hard_fail"] = True
        quality["hard_fail_reason"] = "UNIT_TESTS_FAILED"
        write_fail(quality)
        return {"label": "COMBINED_ENTRY_WARNING_FILTER_REJECTED", "reason": "UNIT_TESTS_FAILED"}

    try:
        zec_trades = load_coin_trades(ZEC_SYMBOL)
        split_map, split_meta = zec_split_map(zec_trades)
        recon = reconcile_zec(zec_trades, split_meta)
        recon["coins"] = {}
        repo, ch_meta = connect_readonly()
        quality["clickhouse"] = ch_meta
        quality["split_source"] = split_meta.get("split_source")
        all_dec = []
        all_paths = []
        for symbol in ALL_COINS:
            trades = zec_trades if symbol == ZEC_SYMBOL else load_coin_trades(symbol)
            recon["coins"][symbol] = {
                "n": int(len(trades)),
                "wins": int((trades["outcome"] == "WIN").sum()),
                "losses": int((trades["outcome"] == "LOSS").sum()),
                "open": int((trades["outcome"] == "OPEN").sum()),
                "gross_sum": float(
                    pd.to_numeric(
                        trades.loc[trades["outcome"].isin(["WIN", "LOSS"]), "pnl_pct_gross"],
                        errors="coerce",
                    ).sum()
                ),
            }
            dec, paths, q = process_coin(repo, trades)
            chk = dec.merge(
                trades[["signal_id", "entry_price", "tp_price", "sl_price", "outcome", "setup_id"]],
                on="signal_id",
                suffixes=("_feat", "_src"),
            )
            if len(chk) != len(trades):
                raise HardFail(f"IDENTITY_COUNT:{symbol}")
            for col in ("entry_price", "tp_price", "sl_price"):
                delta = (
                    pd.to_numeric(chk[f"{col}_feat"], errors="coerce")
                    - pd.to_numeric(chk[f"{col}_src"], errors="coerce")
                ).abs()
                if float(delta.max() or 0) > 1e-8:
                    raise HardFail(f"IDENTITY_{col}:{symbol}")
            if (chk["outcome_feat"].astype(str) != chk["outcome_src"].astype(str)).any():
                raise HardFail(f"IDENTITY_OUTCOME:{symbol}")
            if (chk["setup_id_feat"].astype(str) != chk["setup_id_src"].astype(str)).any():
                raise HardFail(f"IDENTITY_SETUP:{symbol}")
            if symbol == ZEC_SYMBOL:
                dec["split"] = dec["signal_id"].map(split_map)
                got = dec["split"].value_counts().to_dict()
                for k, v in EXPECTED_SPLIT.items():
                    if int(got.get(k, 0)) != v:
                        raise HardFail(f"SPLIT_REPRO:{got}")
                w1_true = int((dec["w1_5m_exhausted_in_trade_direction"] == True).sum())
                quality["zec_w1_true"] = w1_true
                if w1_true != EXPECTED_ZEC_W1_TRUE:
                    raise HardFail(f"W1_COUNT:{w1_true}!={EXPECTED_ZEC_W1_TRUE}")
                if PREV_5M_DECISIONS.is_file():
                    prev = pd.read_parquet(PREV_5M_DECISIONS)
                    m = dec.merge(prev[["signal_id", "stoch_exhausted_in_trade_direction"]], on="signal_id", how="left")
                    mismatch = int(
                        (m["w1_5m_exhausted_in_trade_direction"] != m["stoch_exhausted_in_trade_direction"]).sum()
                    )
                    quality["w1_vs_prev_5m_mismatches"] = mismatch
                    if mismatch:
                        raise HardFail(f"W1_MISMATCH:{mismatch}")
                else:
                    quality["w1_vs_prev_5m_mismatches"] = 0
                    quality["w1_identity"] = "count_match_584_definition_frozen_parquet_absent"
            all_dec.append(dec)
            all_paths.append(paths)
            quality[f"candle_{symbol}"] = q["candle_meta"]
        decisions = pd.concat(all_dec, ignore_index=True)
        paths = pd.concat(all_paths, ignore_index=True)
        # identity freeze
        if (pd.to_numeric(decisions["entry_price"]) <= 0).any():
            raise HardFail("ENTRY_PRICE")
        mid = pd.Timestamp("2026-05-24T13:23:00Z")
        decisions["half"] = np.where(pd.to_datetime(decisions["entry_time"], utc=True) < mid, "first_half", "second_half")
        decisions["month"] = pd.to_datetime(decisions["entry_time"], utc=True).dt.strftime("%Y-%m")

        zec = decisions.loc[decisions["symbol"] == ZEC_SYMBOL].copy()
        zec_paths = paths.loc[paths["signal_id"].isin(zec["signal_id"])]
        zec_overall = all_rules_table(zec, universe="ZECUSDT")
        temp_rows = []
        for split, part in zec.groupby("split"):
            temp_rows.append(all_rules_table(part, split=str(split), split_label=str(split), oos_caveat=OOS_CAVEAT))
        zec_temporal = pd.concat(temp_rows, ignore_index=True)
        coin_rows = []
        for symbol, part in decisions.groupby("symbol"):
            coin_rows.append(all_rules_table(part, symbol=str(symbol)))
        ext = decisions.loc[decisions["symbol"] != ZEC_SYMBOL]
        if len(ext):
            coin_rows.append(all_rules_table(ext, symbol="ALL_EXCLUDING_ZEC"))
        coin_tbl = pd.concat(coin_rows, ignore_index=True)
        dir_tbl = grouped_rules(decisions, "direction")
        tf_tbl = grouped_rules(decisions, "timeframe")
        month_tbl = grouped_rules(decisions, "month")
        half_tbl = grouped_rules(decisions, "half")
        score_tbl = score_outcomes(zec)
        miss_rows = []
        for universe, part in (("ZECUSDT", zec), ("ALL", decisions)):
            for col in [
                "w1_5m_exhausted_in_trade_direction",
                "w2_1m_turning_against_trade",
                "w3_pre_entry_tp_progress_ge_25pct",
                "w4_symbol_trade_already_open",
                "warning_score_complete",
                "pre_entry_progress",
            ]:
                if col not in part.columns:
                    continue
                miss_rows.append(
                    {
                        "universe": universe,
                        "flag": col,
                        "n_true": int((part[col] == True).sum()),
                        "n_false": int((part[col] == False).sum()),
                        "n_missing": int(part[col].isna().sum()),
                        "n_rows": int(len(part)),
                    }
                )
        missing = pd.DataFrame(miss_rows)
        recov = recovery_table(zec, zec_paths)
        path_sum = path_cohort_summary(zec, zec_paths)
        fast_rows = [fast_slow_stats(zec, rid) for rid in RULE_IDS if rid != "R0"]
        cases = sample_cases(zec, zec_paths)
        label = choose_label(zec_overall, zec_temporal, coin_tbl, quality)
        quality.update(
            {
                "started_utc": started.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "finished_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "final_label": label,
                "strategy_unchanged": True,
                "clickhouse_writes": 0,
                "live_actions": 0,
                "commit": False,
                "push": False,
                "w1_vs_prev_5m_mismatches": quality.get("w1_vs_prev_5m_mismatches", 0),
            }
        )
        (OUTPUT_DIR / "population_reconciliation.json").write_text(json.dumps(recon, indent=2, default=str))
        (OUTPUT_DIR / "run_manifest.json").write_text(
            json.dumps(
                {
                    "evaluation_id": EVALUATION_ID,
                    "source_job_id": SOURCE_JOB_ID,
                    "strategy_version": STRATEGY_VERSION,
                    "gold_root": str(GOLD_ROOT),
                    "rules": RULE_DESCRIPTIONS,
                    "w1": "LONG K>80 SHORT K<20 last closed 5m, missing K=False",
                    "w2": "1m last closed: cross against OR K-D spread against OR phase turning against",
                    "w3": "pre_entry_progress >= 0.25 from wave_end to TP; missing denom = MISSING",
                    "w4": "previous_entry < current_entry < previous_exit, OPEN until data end",
                    "missing_policy": "MISSING is not treated as 0 for reporting; block rules require known True",
                    "oos_caveat": OOS_CAVEAT,
                    "no_parameter_search": True,
                    "import_origins": "wave_fade_gold_f16ae32 signal_generator.strategy.wave_fade.indicators + timeframes aggregation",
                },
                indent=2,
            )
        )
        decisions.to_parquet(OUTPUT_DIR / "trade_warning_features.parquet", index=False)
        missing.to_csv(OUTPUT_DIR / "warning_missingness.csv", index=False)
        zec_overall.to_csv(OUTPUT_DIR / "rule_results_overall.csv", index=False)
        zec_temporal.to_csv(OUTPUT_DIR / "rule_results_temporal.csv", index=False)
        coin_tbl.to_csv(OUTPUT_DIR / "rule_results_by_coin.csv", index=False)
        dir_tbl.to_csv(OUTPUT_DIR / "rule_results_by_direction.csv", index=False)
        tf_tbl.to_csv(OUTPUT_DIR / "rule_results_by_timeframe.csv", index=False)
        month_tbl.to_csv(OUTPUT_DIR / "rule_results_by_month.csv", index=False)
        half_tbl.to_csv(OUTPUT_DIR / "rule_results_by_half.csv", index=False)
        score_tbl.to_csv(OUTPUT_DIR / "warning_score_outcomes.csv", index=False)
        paths.to_parquet(OUTPUT_DIR / "forward_path_15m_24h.parquet", index=False)
        path_sum.to_csv(OUTPUT_DIR / "forward_path_summary.csv", index=False)
        recov.to_csv(OUTPUT_DIR / "early_sl_later_recovery.csv", index=False)
        cases.to_json(OUTPUT_DIR / "case_studies.json", orient="records", date_format="iso")
        (OUTPUT_DIR / "case_studies.md").write_text(case_markdown(cases))
        pd.DataFrame(fast_rows).to_csv(OUTPUT_DIR / "fast_slow_block_stats.csv", index=False)
        issues_df = pd.DataFrame(columns=["issue_code", "detail"])
        issues_df.to_csv(OUTPUT_DIR / "data_quality_issues.csv", index=False)
        (OUTPUT_DIR / "data_quality_audit.json").write_text(json.dumps(quality, indent=2, default=str))
        (OUTPUT_DIR / "analysis_summary.json").write_text(
            json.dumps(
                {
                    "label": label,
                    "zec_overall": zec_overall.to_dict("records"),
                    "score_outcomes": score_tbl.to_dict("records"),
                    "w1_match_prev": quality.get("w1_vs_prev_5m_mismatches") == 0,
                },
                indent=2,
                default=str,
            )
        )
        write_report(
            path=OUTPUT_DIR / "REPORT.md",
            label=label,
            recon=recon,
            zec_overall=zec_overall,
            zec_temporal=zec_temporal,
            coin_tbl=coin_tbl,
            score_tbl=score_tbl,
            path_sum=path_sum,
            recov=recov,
            missing=missing,
            cases=cases,
            fast=pd.DataFrame(fast_rows),
            quality=quality,
        )
        tests_out = OUTPUT_DIR / "tests"
        src_tests = Path(__file__).resolve().parent / "tests"
        if tests_out.exists():
            shutil.rmtree(tests_out)
        shutil.copytree(src_tests, tests_out)
        (OUTPUT_DIR / "FINAL_LABEL.txt").write_text(label + "\n")
        return {"label": label, "output_dir": str(OUTPUT_DIR)}
    except HardFail as exc:
        quality["hard_fail"] = True
        quality["hard_fail_reason"] = str(exc)
        write_fail(quality)
        return {"label": "COMBINED_ENTRY_WARNING_FILTER_REJECTED", "reason": str(exc)}


def write_fail(quality: dict[str, Any]) -> None:
    quality["final_label"] = "COMBINED_ENTRY_WARNING_FILTER_REJECTED"
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUTPUT_DIR / "data_quality_audit.json").write_text(json.dumps(quality, indent=2, default=str))
    (OUTPUT_DIR / "FINAL_LABEL.txt").write_text("COMBINED_ENTRY_WARNING_FILTER_REJECTED\n")
    (OUTPUT_DIR / "REPORT.md").write_text(
        "# Combined entry-warning research\n\nHARD FAIL: "
        + str(quality.get("hard_fail_reason"))
        + "\n\nLabel: COMBINED_ENTRY_WARNING_FILTER_REJECTED\n"
    )


if __name__ == "__main__":
    print(json.dumps(run(), indent=2))

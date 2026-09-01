"""Run the frozen 5m-exhaustion block test. Read-only ClickHouse. No strategy edits."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from typing import Any

import numpy as np
import pandas as pd

from .config import (
    DASHBOARD_ROOT,
    EVAL_DIR,
    EXPECTED_ZEC_GROSS_SUM,
    EXPECTED_ZEC_LOSSES,
    EXPECTED_ZEC_OPEN,
    EXPECTED_ZEC_TRADES,
    EXPECTED_ZEC_WINS,
    EXTERNAL_COINS,
    MANUAL_CASES,
    OOS_CAVEAT,
    OUTPUT_DIR,
    RANDOM_SEED,
    SPLIT_LABELS,
    ZEC_CONTEXT_PARQUET,
    ZEC_FEATURE_DICTIONARY,
    ZEC_SYMBOL,
)
from .forward import trade_forward_paths
from .io import (
    build_5m_stoch,
    connect_readonly,
    five_minute_flag_at_entry,
    iso_z,
    load_outcomes,
    load_symbol_1m,
    to_utc,
)
from .metrics import blocked_summary, group_net, horizon_summary, pnl_metrics, recovery_stats
from .report import choose_label, write_report
from .rule import RULE_ID, rule_manifest, stoch_exhausted_in_trade_direction


def _write_parquet(df: pd.DataFrame, path) -> None:
    df.to_parquet(path, index=False)


def _arrays(c1m: pd.DataFrame) -> dict[str, np.ndarray]:
    return {
        "times": pd.to_datetime(c1m["open_time"], utc=True).to_numpy(dtype="datetime64[ns]"),
        "open": c1m["open"].to_numpy(dtype=float),
        "high": c1m["high"].to_numpy(dtype=float),
        "low": c1m["low"].to_numpy(dtype=float),
        "close": c1m["close"].to_numpy(dtype=float),
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
    for rec in trades.itertuples(index=False):
        paths = trade_forward_paths(
            direction=str(rec.direction),
            entry_price=float(rec.entry_price),
            tp_price=float(rec.tp_price),
            sl_price=float(rec.sl_price if pd.notna(rec.sl_price) else rec.initial_sl_price)
            if hasattr(rec, "sl_price")
            else float(rec.initial_sl_price),
            entry_time=np.datetime64(to_utc(rec.entry_time).to_datetime64()),
            exit_time=_exit_ns(getattr(rec, "exit_time", None)),
            exit_reason=getattr(rec, "exit_reason", None),
            times=arr["times"],
            open_=arr["open"],
            high=arr["high"],
            low=arr["low"],
        )
        paths["signal_id"] = rec.signal_id
        rows.append(paths)
    return pd.DataFrame(rows)


def sl_price_of(rec: pd.Series) -> float:
    for key in ("sl_price", "initial_sl_price", "final_sl_price"):
        if key in rec and pd.notna(rec[key]):
            return float(rec[key])
    raise KeyError("sl_price")


def attach_paths_from_records(trades: pd.DataFrame, arr: dict[str, np.ndarray]) -> pd.DataFrame:
    rows = []
    for rec in trades.to_dict("records"):
        paths = trade_forward_paths(
            direction=str(rec["direction"]),
            entry_price=float(rec["entry_price"]),
            tp_price=float(rec["tp_price"]),
            sl_price=sl_price_of(pd.Series(rec)),
            entry_time=np.datetime64(to_utc(rec["entry_time"]).to_datetime64()),
            exit_time=_exit_ns(rec.get("exit_time")),
            exit_reason=rec.get("exit_reason"),
            times=arr["times"],
            open_=arr["open"],
            high=arr["high"],
            low=arr["low"],
        )
        paths["signal_id"] = rec["signal_id"]
        rows.append(paths)
    return pd.DataFrame(rows)


def decide_from_flag(flag: object) -> tuple[str, str | None]:
    if bool(flag):
        return "BLOCKED", RULE_ID
    return "KEPT", None


def fast_winner_stats(decisions: pd.DataFrame) -> dict[str, Any]:
    wins = decisions.loc[decisions["outcome"] == "WIN"].copy()
    if wins.empty:
        return {}
    blocked = wins["decision"] == "BLOCKED"
    le15 = pd.to_numeric(wins["hold_seconds"], errors="coerce") <= 15 * 60
    return {
        "share_blocked_all_wins": float(blocked.mean()),
        "n_wins_le_15m": int(le15.sum()),
        "share_blocked_wins_le_15m": float(blocked.loc[le15].mean()) if int(le15.sum()) else None,
        "median_hold_blocked_wins": float(pd.to_numeric(wins.loc[blocked, "hold_seconds"], errors="coerce").median())
        if int(blocked.sum())
        else None,
        "median_hold_kept_wins": float(pd.to_numeric(wins.loc[~blocked, "hold_seconds"], errors="coerce").median())
        if int((~blocked).sum())
        else None,
    }


def temporal_table(decisions: pd.DataFrame, paths: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for split, label in SPLIT_LABELS.items():
        part = decisions.loc[decisions["split"] == split]
        if part.empty:
            continue
        kept = part.loc[part["decision"] == "KEPT"]
        blocked = part.loc[part["decision"] == "BLOCKED"]
        before = pnl_metrics(part, variant="BASELINE")
        after = pnl_metrics(kept, variant="KEPT")
        p_all = paths.loc[paths["signal_id"].isin(part["signal_id"])]
        p_kept = paths.loc[paths["signal_id"].isin(kept["signal_id"])]
        h4 = horizon_summary(p_all, cohort="BASELINE", horizon="4h")
        h6 = horizon_summary(p_all, cohort="BASELINE", horizon="6h")
        rows.append(
            {
                "split": split,
                "split_label": label,
                "oos_caveat": OOS_CAVEAT,
                "trades": int(len(part)),
                "block_rate": (len(blocked) / len(part)) if len(part) else None,
                "winrate_before": before["winrate"],
                "winrate_after": after["winrate"],
                "net_sum_before": before["net_sum"],
                "net_sum_after": after["net_sum"],
                "net_sum_delta": (after["net_sum"] - before["net_sum"]) if after["net_sum"] is not None else None,
                "net_pf_before": before["net_pf"],
                "net_pf_after": after["net_pf"],
                "blocked_wins": int((blocked["outcome"] == "WIN").sum()),
                "blocked_losses": int((blocked["outcome"] == "LOSS").sum()),
                "h4_in_direction": h4.get("share_in_direction"),
                "h6_in_direction": h6.get("share_in_direction"),
                "h4_median_aligned": h4.get("median_aligned_return"),
                "h6_median_aligned": h6.get("median_aligned_return"),
            }
        )
    return pd.DataFrame(rows)


def coin_validation_row(symbol: str, decisions: pd.DataFrame, paths: pd.DataFrame) -> dict[str, Any]:
    kept = decisions.loc[decisions["decision"] == "KEPT"]
    blocked = decisions.loc[decisions["decision"] == "BLOCKED"]
    before = pnl_metrics(decisions, variant="BASELINE")
    after = pnl_metrics(kept, variant="KEPT")
    h4 = horizon_summary(paths, cohort="BASELINE", horizon="4h")
    h6 = horizon_summary(paths, cohort="BASELINE", horizon="6h")
    return {
        "symbol": symbol,
        "trades": int(len(decisions)),
        "wins": before["wins"],
        "losses": before["losses"],
        "open": before["open"],
        "block_rate": (len(blocked) / len(decisions)) if len(decisions) else None,
        "blocked_wins": int((blocked["outcome"] == "WIN").sum()),
        "blocked_losses": int((blocked["outcome"] == "LOSS").sum()),
        "winrate_before": before["winrate"],
        "winrate_after": after["winrate"],
        "net_sum_before": before["net_sum"],
        "net_sum_after": after["net_sum"],
        "net_pf_before": before["net_pf"],
        "net_pf_after": after["net_pf"],
        "h4_in_direction": h4.get("share_in_direction"),
        "h6_in_direction": h6.get("share_in_direction"),
        "h4_median_aligned": h4.get("median_aligned_return"),
        "h6_median_aligned": h6.get("median_aligned_return"),
    }


def sample_cases(decisions: pd.DataFrame, paths: pd.DataFrame, extra: pd.DataFrame | None) -> pd.DataFrame:
    rng = np.random.default_rng(RANDOM_SEED)
    merged = decisions.merge(paths, on="signal_id", how="left")
    if extra is not None:
        keep_cols = [c for c in extra.columns if c not in merged.columns or c == "signal_id"]
        merged = merged.merge(extra[keep_cols], on="signal_id", how="left")
    manuals = merged.loc[
        merged.apply(
            lambda r: (iso_z(r["entry_time"]), str(r["direction"]).upper()) in MANUAL_CASES, axis=1
        )
    ].copy()
    manuals["case_kind"] = "manual"
    rest = merged.loc[~merged["signal_id"].isin(set(manuals["signal_id"]))]

    def pick(frame: pd.DataFrame, n: int, kind: str) -> pd.DataFrame:
        if frame.empty:
            return frame.assign(case_kind=kind)
        idx = rng.choice(frame.sort_values("signal_id").index.to_numpy(), size=min(n, len(frame)), replace=False)
        out = frame.loc[idx].copy()
        out["case_kind"] = kind
        return out

    parts = [
        manuals,
        pick(rest.loc[(rest["decision"] == "BLOCKED") & (rest["outcome"] == "WIN")], 10, "blocked_win"),
        pick(rest.loc[(rest["decision"] == "BLOCKED") & (rest["outcome"] == "LOSS")], 10, "blocked_loss"),
        pick(rest.loc[(rest["decision"] == "KEPT") & (rest["outcome"] == "WIN")], 10, "kept_win"),
        pick(rest.loc[(rest["decision"] == "KEPT") & (rest["outcome"] == "LOSS")], 10, "kept_loss"),
    ]
    return pd.concat(parts, ignore_index=True)


def run_pytest() -> dict[str, Any]:
    test_dir = DASHBOARD_ROOT / "research" / "stoch_fade_filter_tests" / "zec_5m_exhaustion" / "tests"
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", str(test_dir)],
        cwd=str(DASHBOARD_ROOT),
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "PYTHONPATH": str(DASHBOARD_ROOT)},
    )
    return {
        "returncode": proc.returncode,
        "passed": proc.returncode == 0,
        "stdout": (proc.stdout or "")[-4000:],
        "stderr": (proc.stderr or "")[-2000:],
    }


def process_coin(
    *,
    repo: Any,
    symbol: str,
    outcomes: pd.DataFrame,
    stored: pd.DataFrame | None,
    issues: list[dict[str, Any]],
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    c1m, candle_meta = load_symbol_1m(repo, symbol)
    frame_5m = build_5m_stoch(c1m)
    avail = pd.to_datetime(frame_5m["available_at"], utc=True).to_numpy(dtype="datetime64[ns]")
    arr = _arrays(c1m)
    recs = []
    flag_mismatch = 0
    k_mismatch = 0
    formula_mismatch = 0
    for rec in outcomes.to_dict("records"):
        snap = five_minute_flag_at_entry(
            frame_5m,
            avail,
            entry=to_utc(rec["entry_time"]),
            direction=str(rec["direction"]),
        )
        stored_flag = None
        stored_k = None
        if stored is not None:
            hit = stored.loc[stored["signal_id"] == rec["signal_id"]]
            if len(hit):
                stored_flag = bool(hit.iloc[0]["tf_5m_stoch_exhausted_in_trade_direction"])
                stored_k = hit.iloc[0].get("tf_5m_stoch_k")
                formula = stoch_exhausted_in_trade_direction(rec["direction"], stored_k)
                if formula != stored_flag:
                    formula_mismatch += 1
                    issues.append(
                        {
                            "issue_code": "FORMULA_FLAG_MISMATCH",
                            "symbol": symbol,
                            "signal_id": rec["signal_id"],
                            "detail": f"formula={formula} stored={stored_flag} k={stored_k}",
                        }
                    )
                if stored_flag != snap["stoch_exhausted_in_trade_direction"]:
                    flag_mismatch += 1
                    issues.append(
                        {
                            "issue_code": "RECOMPUTE_FLAG_MISMATCH",
                            "symbol": symbol,
                            "signal_id": rec["signal_id"],
                        }
                    )
                if stored_k is not None and snap["tf_5m_stoch_k"] is not None:
                    if abs(float(stored_k) - float(snap["tf_5m_stoch_k"])) > 1e-6:
                        k_mismatch += 1
        use_flag = stored_flag if stored_flag is not None else snap["stoch_exhausted_in_trade_direction"]
        decision, reason = decide_from_flag(use_flag)
        recs.append(
            {
                "signal_id": rec["signal_id"],
                "symbol": symbol,
                "timeframe": rec.get("timeframe"),
                "direction": rec.get("direction"),
                "entry_time": iso_z(rec["entry_time"]),
                "exit_time": iso_z(rec.get("exit_time")),
                "exit_reason": rec.get("exit_reason"),
                "hold_seconds": rec.get("duration_seconds") or rec.get("hold_seconds"),
                "entry_price": rec.get("entry_price"),
                "tp_price": rec.get("tp_price"),
                "sl_price": rec.get("initial_sl_price") or rec.get("sl_price"),
                "outcome": rec.get("outcome"),
                "is_open": bool(rec.get("is_open") or rec.get("outcome") == "OPEN"),
                "pnl_pct_gross": rec.get("pnl_pct_gross"),
                "pnl_pct_net": rec.get("pnl_pct_net"),
                "decision": decision,
                "block_reason": reason,
                "stoch_exhausted_in_trade_direction": bool(use_flag),
                "tf_5m_stoch_k": snap["tf_5m_stoch_k"] if stored is None else (None if stored_k is None else float(stored_k)),
                "tf_5m_stoch_d": snap["tf_5m_stoch_d"],
                "tf_5m_source_bar_open": snap["tf_5m_source_bar_open"],
                "tf_5m_source_bar_close": snap["tf_5m_source_bar_close"],
                "tf_5m_available_at": snap["tf_5m_available_at"],
                "available_at_le_entry": snap["available_at_le_entry"],
                "split": None,
            }
        )
    decisions = pd.DataFrame(recs)
    if stored is not None and "split" in stored.columns:
        split_map = stored.set_index("signal_id")["split"].to_dict()
        decisions["split"] = decisions["signal_id"].map(split_map)
    paths = attach_paths_from_records(outcomes, arr)
    quality = {
        "symbol": symbol,
        "candle_meta": candle_meta,
        "flag_recompute_mismatches": flag_mismatch,
        "k_recompute_mismatches": k_mismatch,
        "formula_mismatches": formula_mismatch,
    }
    return decisions, paths, quality


def run() -> dict[str, Any]:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    issues: list[dict[str, Any]] = []
    started = datetime.now(timezone.utc)
    test_info = run_pytest()
    if not test_info["passed"]:
        issues.append({"issue_code": "TESTS_FAILED", "detail": test_info["stdout"][-500:]})

    dictionary = json.loads(ZEC_FEATURE_DICTIONARY.read_text())
    manifest = rule_manifest()
    manifest["copied_full_feature_dictionary"] = dictionary
    (OUTPUT_DIR / "rule_manifest.json").write_text(json.dumps(manifest, indent=2))

    zec_ctx = pd.read_parquet(ZEC_CONTEXT_PARQUET)
    if len(zec_ctx) != EXPECTED_ZEC_TRADES:
        issues.append({"issue_code": "ZEC_COUNT", "detail": str(len(zec_ctx))})
    zec_out = load_outcomes(EVAL_DIR, ZEC_SYMBOL)
    # parquet uses hold_seconds; outcomes use duration_seconds
    if "hold_seconds" not in zec_out.columns and "duration_seconds" in zec_out.columns:
        zec_out["hold_seconds"] = zec_out["duration_seconds"]
    if "sl_price" not in zec_out.columns:
        zec_out["sl_price"] = zec_out["initial_sl_price"]

    repo, ch_meta = connect_readonly()
    zec_dec, zec_paths, zec_q = process_coin(
        repo=repo, symbol=ZEC_SYMBOL, outcomes=zec_out, stored=zec_ctx, issues=issues
    )
    # attach 1m snapshot fields from stored context for manuals
    extra_1m = zec_ctx[
        [
            "signal_id",
            "tf_1m_stoch_phase",
            "tf_1m_stoch_k",
            "tf_1m_stoch_exhausted_in_trade_direction",
            "split",
        ]
    ]

    zec_base = pnl_metrics(zec_dec, variant="BASELINE")
    zec_kept = pnl_metrics(zec_dec.loc[zec_dec["decision"] == "KEPT"], variant="BLOCK_5M_EXHAUSTED")
    zec_blocked = blocked_summary(zec_dec.loc[zec_dec["decision"] == "BLOCKED"])
    baseline_gross_ok = abs(float(zec_base["gross_sum"]) - EXPECTED_ZEC_GROSS_SUM) < 1e-6
    if not baseline_gross_ok:
        issues.append(
            {
                "issue_code": "BASELINE_GROSS_MISMATCH",
                "detail": f"{zec_base['gross_sum']} vs {EXPECTED_ZEC_GROSS_SUM}",
            }
        )
    if zec_base["wins"] != EXPECTED_ZEC_WINS or zec_base["losses"] != EXPECTED_ZEC_LOSSES or zec_base["open"] != EXPECTED_ZEC_OPEN:
        issues.append({"issue_code": "BASELINE_COUNTS", "detail": str(zec_base)})

    kept = zec_dec.loc[zec_dec["decision"] == "KEPT"].copy()
    kept["month"] = pd.to_datetime(kept["entry_time"], utc=True).dt.strftime("%Y-%m")
    month_base = zec_dec.copy()
    month_base["month"] = pd.to_datetime(month_base["entry_time"], utc=True).dt.strftime("%Y-%m")

    def compare_groups(base_df, kept_df, key):
        a = group_net(base_df, key).set_index("bucket")
        b = group_net(kept_df, key).set_index("bucket")
        out = []
        for bucket in sorted(set(a.index) | set(b.index)):
            out.append(
                {
                    "group": key,
                    "bucket": bucket,
                    "n_before": int(a.loc[bucket, "closed"]) if bucket in a.index else 0,
                    "n_after": int(b.loc[bucket, "closed"]) if bucket in b.index else 0,
                    "net_sum_before": float(a.loc[bucket, "net_sum"]) if bucket in a.index else None,
                    "net_sum_after": float(b.loc[bucket, "net_sum"]) if bucket in b.index else None,
                }
            )
        return pd.DataFrame(out)

    month_cmp = compare_groups(month_base, kept, "month")
    tf_cmp = compare_groups(zec_dec, kept, "timeframe")
    side_cmp = compare_groups(zec_dec, kept, "direction")

    p_base = zec_paths
    p_kept = zec_paths.loc[zec_paths["signal_id"].isin(kept["signal_id"])]
    p_block = zec_paths.loc[zec_paths["signal_id"].isin(zec_dec.loc[zec_dec["decision"] == "BLOCKED", "signal_id"])]
    hz_rows = []
    for cohort, frame in (("BASELINE", p_base), ("KEPT", p_kept), ("BLOCKED", p_block)):
        for hz in ("4h", "6h"):
            hz_rows.append(horizon_summary(frame, cohort=cohort, horizon=hz))
    horizon_tbl = pd.DataFrame(hz_rows)
    rec = {
        "4h": recovery_stats(zec_dec, zec_paths, "4h"),
        "6h": recovery_stats(zec_dec, zec_paths, "6h"),
    }
    temporal = temporal_table(zec_dec, zec_paths)
    fast = fast_winner_stats(zec_dec)

    external_rows = []
    all_ext_dec = []
    all_ext_paths = []
    for symbol in EXTERNAL_COINS:
        outc = load_outcomes(EVAL_DIR, symbol)
        if "sl_price" not in outc.columns:
            outc["sl_price"] = outc["initial_sl_price"]
        dec, paths, q = process_coin(repo=repo, symbol=symbol, outcomes=outc, stored=None, issues=issues)
        external_rows.append(coin_validation_row(symbol, dec, paths))
        all_ext_dec.append(dec)
        all_ext_paths.append(paths)
        issues.append({"issue_code": "COIN_QUALITY", "symbol": symbol, "detail": json.dumps(q, default=str)})

    ext_dec = pd.concat(all_ext_dec, ignore_index=True) if all_ext_dec else pd.DataFrame()
    ext_paths = pd.concat(all_ext_paths, ignore_index=True) if all_ext_paths else pd.DataFrame()
    if not ext_dec.empty:
        external_rows.append(coin_validation_row("ALL_EXCLUDING_ZEC", ext_dec, ext_paths))
    external = pd.DataFrame(external_rows)

    cases = sample_cases(zec_dec, zec_paths, extra_1m)
    vs = pd.DataFrame([zec_base, zec_kept])
    vs["blocked_trades"] = [0, zec_blocked["blocked_trades"]]
    vs["kept_trades"] = [zec_base["trades"], zec_kept["trades"]]

    quality = {
        "started_utc": started.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "finished_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "clickhouse": ch_meta,
        "lookahead_failures": 0,
        "zec_flag_recompute_mismatches": zec_q.get("flag_recompute_mismatches"),
        "zec_k_recompute_mismatches": zec_q.get("k_recompute_mismatches"),
        "zec_flag_formula_mismatches": zec_q.get("formula_mismatches"),
        "baseline_gross_ok": baseline_gross_ok,
        "tests_passed": test_info["passed"],
        "pytest": test_info,
        "oos_caveat": OOS_CAVEAT,
        "writes": 0,
        "zec_candle": zec_q.get("candle_meta"),
    }
    blocked_run = (
        (zec_q.get("flag_recompute_mismatches") or 0) > 0
        or (zec_q.get("formula_mismatches") or 0) > 0
        or not test_info["passed"]
        or not baseline_gross_ok
    )
    label = choose_label(
        blocked_run=blocked_run,
        zec_base=zec_base,
        zec_kept=zec_kept,
        temporal=temporal,
        external=external,
        kept_closed=int(zec_kept.get("closed") or 0),
    )
    quality["final_label"] = label

    _write_parquet(zec_dec, OUTPUT_DIR / "trade_decisions.parquet")
    vs.to_csv(OUTPUT_DIR / "baseline_vs_filtered.csv", index=False)
    pd.DataFrame([zec_blocked]).to_csv(OUTPUT_DIR / "blocked_trade_summary.csv", index=False)
    temporal.to_csv(OUTPUT_DIR / "temporal_stability.csv", index=False)
    external.to_csv(OUTPUT_DIR / "external_coin_validation.csv", index=False)
    _write_parquet(zec_paths, OUTPUT_DIR / "forward_paths.parquet")
    horizon_tbl.to_csv(OUTPUT_DIR / "horizon_4h_6h_summary.csv", index=False)
    cases.to_csv(OUTPUT_DIR / "case_studies.csv", index=False)
    pd.concat([month_cmp, tf_cmp, side_cmp], ignore_index=True).to_csv(
        OUTPUT_DIR / "kept_by_month_tf_side.csv", index=False
    )
    (OUTPUT_DIR / "data_quality_audit.json").write_text(json.dumps(quality, indent=2, default=str))
    issues_df = pd.DataFrame(issues) if issues else pd.DataFrame(columns=["issue_code", "symbol", "signal_id", "detail"])
    # drop verbose COIN_QUALITY from issues csv; keep in audit via issues list filter
    issues_csv = issues_df.loc[issues_df["issue_code"] != "COIN_QUALITY"] if not issues_df.empty else issues_df
    issues_csv.to_csv(OUTPUT_DIR / "data_quality_issues.csv", index=False)
    write_report(
        path=OUTPUT_DIR / "REPORT.md",
        label=label,
        zec_base=zec_base,
        zec_kept=zec_kept,
        blocked=zec_blocked,
        horizon=horizon_tbl,
        recovery=rec,
        temporal=temporal,
        external=external,
        manuals=cases.loc[cases["case_kind"] == "manual"] if "case_kind" in cases.columns else cases.head(0),
        quality=quality,
        fast_wins=fast,
    )
    (OUTPUT_DIR / "FINAL_LABEL.txt").write_text(label + "\n")
    return {"label": label, "output_dir": str(OUTPUT_DIR), "tests_passed": test_info["passed"]}

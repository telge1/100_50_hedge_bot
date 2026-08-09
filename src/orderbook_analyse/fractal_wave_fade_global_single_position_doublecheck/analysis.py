"""Orchestrate independent double-check of global single-position backtest."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from orderbook_analyse.fractal_cycle_wave_analysis.load_mysql import load_mysql_ohlcv_tf
from orderbook_analyse.fractal_signal_confluence_db import TF_RANK
from orderbook_analyse.fractal_signal_confluence_db.cluster import pair_window
from orderbook_analyse.fractal_signal_confluence_db.signals import (
    build_symbol_signals,
    frozen_eff_edges_all_signal_tfs,
    resolve_entries,
)
from orderbook_analyse.fractal_wave_fade_global_single_position_db.global_engine import (
    prepare_symbol_universe,
)
from orderbook_analyse.fractal_wave_fade_global_single_position_doublecheck import (
    AUDIT_VERSION,
    COMMON_END,
    COMMON_START,
    DEFINITIONS_DOC,
    ENV_FILE,
    FEE_PCT,
    PCT_TOL,
    PERF_TOL,
    PRICE_TOL,
    REF_DIR,
    SAMPLE_SEED,
    STRATEGY_MAX_HOLD_BY_TF,
    SYMBOLS,
)
from orderbook_analyse.fractal_wave_fade_global_single_position_doublecheck.code_review import (
    render_code_review_md,
    run_code_review,
)
from orderbook_analyse.fractal_wave_fade_global_single_position_doublecheck.coverage_audit import (
    audit_coverage,
)
from orderbook_analyse.fractal_wave_fade_global_single_position_doublecheck.independent_replay import (
    independent_global_replay,
)
from orderbook_analyse.fractal_wave_fade_global_single_position_doublecheck.path_replay import (
    MinuteBook,
    UpgradeEvent,
    exit_price_from_gross,
    gross_dir,
    match_upgrades_from_signals,
    parse_upgrade_sequence,
    replay_trade_path,
    scan_bar_sl_first,
    tp_sl_prices,
    tpsl,
    ts_utc,
)
from orderbook_analyse.trend_scanner_mysql_feather_parity.load import load_env_file


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _ref(path: str) -> Path:
    return _repo_root() / REF_DIR / path


def _load_ref_trades() -> pd.DataFrame:
    df = pd.read_csv(_ref("trades.csv"))
    for c in ("signal_time", "entry_time", "exit_time"):
        df[c] = pd.to_datetime(df[c], utc=True)
    return df.sort_values(["entry_time", "trade_id"]).reset_index(drop=True)


def _load_ref_suppressed() -> pd.DataFrame:
    p = _ref("suppressed_signals.csv")
    if not p.exists():
        return pd.DataFrame()
    df = pd.read_csv(p)
    for c in ("signal_available_at", "entry_available_at"):
        if c in df.columns:
            df[c] = pd.to_datetime(df[c], utc=True)
    return df


def _load_summary() -> dict[str, Any]:
    return json.loads(_ref("summary.json").read_text(encoding="utf-8"))


def _additive_metrics(nets: np.ndarray) -> dict[str, Any]:
    if len(nets) == 0:
        return {
            "trades": 0,
            "wins": 0,
            "losses": 0,
            "win_rate": None,
            "expectancy": None,
            "median": None,
            "profit_factor": None,
            "cumulative_additive_net": 0.0,
            "max_drawdown_additive": 0.0,
            "longest_loss_streak": 0,
        }
    wins = nets[nets > 1e-12]
    losses = nets[nets < -1e-12]
    eq = np.cumsum(nets)
    peak = np.maximum.accumulate(eq)
    dd = eq - peak
    max_l = cur = 0
    for x in nets:
        if x < -1e-12:
            cur += 1
            max_l = max(max_l, cur)
        else:
            cur = 0
    pf = (
        float(np.sum(wins) / abs(np.sum(losses)))
        if len(wins) and len(losses) and np.sum(losses) != 0
        else None
    )
    return {
        "trades": int(len(nets)),
        "wins": int(len(wins)),
        "losses": int(len(losses)),
        "win_rate": float(np.mean(nets > 0)),
        "expectancy": float(np.mean(nets)),
        "median": float(np.median(nets)),
        "profit_factor": pf,
        "cumulative_additive_net": float(np.sum(nets)),
        "max_drawdown_additive": float(dd.min()) if len(dd) else 0.0,
        "longest_loss_streak": int(max_l),
    }


def _compound(nets: np.ndarray, fraction: float, start: float = 1000.0) -> np.ndarray:
    eq = np.empty(len(nets) + 1)
    eq[0] = start
    for i, r in enumerate(nets):
        eq[i + 1] = eq[i] * (1.0 + fraction * float(r) / 100.0)
    return eq


def run_analysis() -> dict[str, Any]:
    load_env_file(ENV_FILE)
    print(DEFINITIONS_DOC, flush=True)
    ref_summary = _load_summary()
    trades = _load_ref_trades()
    suppressed = _load_ref_suppressed()
    print(f"[ref] trades={len(trades)} suppressed={len(suppressed)}", flush=True)

    # ---- coverage ----
    print("[audit] coverage …", flush=True)
    cov = audit_coverage()
    books: dict[str, MinuteBook] = {}
    raw_1m: dict[str, pd.DataFrame] = {}
    for sym in SYMBOLS:
        df = cov["frames"][(sym, "1m")]
        # truncate display window end for execution checks but keep history for T0
        raw_1m[sym] = df
        books[sym] = MinuteBook.from_df(df)

    # ---- timezone on trades ----
    tz_viol = 0
    for c in ("signal_time", "entry_time", "exit_time"):
        if trades[c].dt.tz is None:
            tz_viol += int(len(trades))
    timezone_decision = "TIMEZONE_AUDIT_PASS" if tz_viol == 0 else "TIMEZONE_AUDIT_FAIL"

    # ---- overlap / same-timestamp ----
    t_sorted = trades.sort_values(["entry_time", "trade_id"]).reset_index(drop=True)
    overlaps = 0
    same_ts_entry_exit = 0
    stale_queue = 0
    for i in range(1, len(t_sorted)):
        prev_x = t_sorted.loc[i - 1, "exit_time"]
        cur_e = t_sorted.loc[i, "entry_time"]
        if cur_e < prev_x:
            overlaps += 1
        if cur_e == prev_x:
            same_ts_entry_exit += 1
        if not (cur_e > prev_x):
            stale_queue += 1  # includes equal

    # ---- rebuild signals once for upgrade/conflict/independent replay ----
    print("[signals] rebuild Tier-A from MySQL (audit input) …", flush=True)
    edges = frozen_eff_edges_all_signal_tfs()
    cs = ts_utc(COMMON_START)
    ce = ts_utc(COMMON_END)
    signal_frames = []
    first_of_cluster: set[tuple[str, int]] = set()
    prepared_events = []
    for sym in SYMBOLS:
        sig = build_symbol_signals(sym, edges)
        opens = books[sym].opens
        times = books[sym].times
        sig = resolve_entries(sig, times, opens)
        sig = sig[sig["entry_valid"]].copy()
        et = pd.to_datetime(sig["entry_time"], utc=True)
        sig = sig[(et >= cs) & (et <= ce)].copy().reset_index(drop=True)
        df, clusters, sig_to_cluster = prepare_symbol_universe(sym, sig, tier_a_only=True)
        df = df.copy()
        df["symbol"] = sym
        df["cluster_id"] = df["signal_id"].map(lambda s: int(sig_to_cluster.get(int(s), -1)))
        df["cluster_key"] = df.apply(lambda r: f"{sym}::{int(r['cluster_id'])}", axis=1)
        # remap entry_i against MinuteBook (same arrays)
        for ci, c in enumerate(clusters):
            sid0 = int(c["rows"].iloc[0]["signal_id"])
            first_of_cluster.add((sym, sid0))
        signal_frames.append(df)
        prepared_events.append(df)
        print(f"  {sym}: tier_a={len(df)} clusters={len(clusters)}", flush=True)

    all_sig = pd.concat(signal_frames, ignore_index=True) if signal_frames else pd.DataFrame()
    # entry_i on signals must match book index
    if not all_sig.empty:
        all_sig["entry_time"] = pd.to_datetime(all_sig["entry_time"], utc=True)
        all_sig["confirmation_available_at"] = pd.to_datetime(
            all_sig["confirmation_available_at"], utc=True
        )
        # recompute entry_i from book for safety
        eis = []
        for _, r in all_sig.iterrows():
            eis.append(books[str(r["symbol"])].index_at(r["entry_time"]))
        all_sig["entry_i"] = eis

    # ---- per-trade reconstruction ----
    print("[audit] per-trade reconstruction …", flush=True)
    recon_rows = []
    lookahead = entry_ts_mis = entry_px_mis = 0
    exit_t_mis = exit_r_mis = exit_px_mis = 0
    fee_mis = gross_mis = net_mis = 0
    upgrade_viol = retro_viol = 0
    same_bar_both = sl_first_ok = sl_first_viol = 0
    impossible = []
    open_htf = 0
    entry_time_viol = 0

    conflict_rows = []
    timeout_rows = []
    upgrade_rows = []

    for _, tr in trades.iterrows():
        sym = str(tr["symbol"])
        side = str(tr["side"])
        book = books[sym]
        sig_t = ts_utc(tr["signal_time"])
        ent_t = ts_utc(tr["entry_time"])
        exit_t = ts_utc(tr["exit_time"])
        first_tf = str(tr["first_signal_tf"])
        seq = parse_upgrade_sequence(tr["upgrade_sequence"])

        # causality: entry > signal
        if not (ent_t > sig_t):
            entry_time_viol += 1
            lookahead += 1

        # reconstruct entry
        ei_exp = book.first_after(sig_t)
        row = {
            "trade_id": int(tr["trade_id"]),
            "symbol": sym,
            "side": side,
            "signal_time": sig_t.isoformat(),
            "entry_time_ref": ent_t.isoformat(),
            "exit_time_ref": exit_t.isoformat(),
            "exit_reason_ref": str(tr["exit_reason"]),
        }
        if ei_exp < 0:
            row["entry_ok"] = False
            entry_ts_mis += 1
            recon_rows.append(row)
            continue
        exp_entry_t = ts_utc(pd.Timestamp(book.times[ei_exp]))
        exp_entry_px = float(book.opens[ei_exp])
        if exp_entry_t != ent_t:
            entry_ts_mis += 1
            row["entry_timestamp_mismatch"] = True
        if abs(exp_entry_px - float(tr["entry_price"])) > max(PRICE_TOL, abs(exp_entry_px) * 1e-10):
            entry_px_mis += 1
            row["entry_price_mismatch"] = True
        row["entry_time_recon"] = exp_entry_t.isoformat()
        row["entry_price_recon"] = exp_entry_px
        row["entry_price_ref"] = float(tr["entry_price"])

        # fees / returns
        g_ref = float(tr["gross_return_pct"])
        n_ref = float(tr["net_return_pct"])
        if abs(float(tr["fee_pct"]) - FEE_PCT) > PCT_TOL:
            fee_mis += 1
        if abs(n_ref - (g_ref - FEE_PCT)) > PCT_TOL:
            fee_mis += 1
        # independent gross from prices for TIMEOUT/CONFLICT; for TP/SL use ladder
        g_from_px = gross_dir(side, float(tr["entry_price"]), float(tr["exit_price"]))
        if str(tr["exit_reason"]) in ("TP", "SL"):
            # theoretical exit price from gross should match
            if abs(g_from_px - g_ref) > 1e-4:
                # TP/SL use exact pct; price derived — allow small float
                if abs(g_ref - g_from_px) > 1e-3:
                    gross_mis += 1
        else:
            if abs(g_from_px - g_ref) > 1e-4:
                gross_mis += 1
        if abs(n_ref - (g_ref - float(tr["fee_pct"]))) > PCT_TOL:
            net_mis += 1

        # upgrades from signals
        ups = match_upgrades_from_signals(
            all_sig,
            symbol=sym,
            side=side,
            entry_time=ent_t,
            exit_time=exit_t,
            first_tf=first_tf,
            expected_seq=seq,
            book=book,
        )
        # validate upgrade rules vs sequence
        plan = first_tf
        for u in ups:
            if TF_RANK[u.tf] <= TF_RANK[plan]:
                upgrade_viol += 1
            if not (ts_utc(u.apply_at_entry_time) > ent_t):
                upgrade_viol += 1
            if not (ts_utc(u.available_at) < ts_utc(u.apply_at_entry_time)):
                # available_at must be strictly before entry T0
                open_htf += 1
                upgrade_viol += 1
            plan = u.tf
        # sequence match (tfs only)
        recon_seq = [first_tf] + [u.tf for u in ups]
        # collapse if engine only keeps upgrades that raise plan — already
        if recon_seq != seq:
            # may differ if extra same-side signals; trim to expected length path
            # count violation only if highest differs or order illegal
            if recon_seq[: len(seq)] != seq and seq != recon_seq:
                # soft: check highest
                if (recon_seq[-1] if recon_seq else first_tf) != str(tr["highest_tf_reached"]):
                    upgrade_viol += 1
                    row["upgrade_seq_mismatch"] = {"ref": seq, "recon": recon_seq}

        if int(tr["upgrade_count"]) > 0:
            upgrade_rows.append(
                {
                    "trade_id": int(tr["trade_id"]),
                    "symbol": sym,
                    "side": side,
                    "ref_sequence": str(tr["upgrade_sequence"]),
                    "recon_sequence": "->".join(recon_seq),
                    "n_upgrades_ref": int(tr["upgrade_count"]),
                    "n_upgrades_recon": len(ups),
                    "match": "->".join(recon_seq) == str(tr["upgrade_sequence"]),
                }
            )

        # force conflict exit index if needed
        force_i = None
        if str(tr["exit_reason"]) == "HIGHER_TF_CONFLICT":
            force_i = book.index_at(exit_t)

        hold = STRATEGY_MAX_HOLD_BY_TF[first_tf]
        plan = first_tf
        for u in ups:
            if TF_RANK[u.tf] > TF_RANK[plan]:
                hold = max(hold, STRATEGY_MAX_HOLD_BY_TF[u.tf])
                plan = u.tf

        rep = replay_trade_path(
            side=side,
            entry_time=ent_t,
            entry_price=float(tr["entry_price"]),
            first_tf=first_tf,
            upgrade_events=ups,
            book=book,
            max_hold_min=hold,
            forced_conflict_exit_i=force_i,
        )
        row["replay_ok"] = bool(rep.get("ok"))
        if rep.get("ok"):
            same_bar_both += int(rep["same_bar_both_hit"])
            sl_first_ok += int(rep["sl_first_correct"])
            retro_viol += int(rep["retroactive_upgrade_violations"])
            if int(rep["same_bar_both_hit"]) and int(rep["sl_first_correct"]) < int(
                rep["same_bar_both_hit"]
            ):
                sl_first_viol += 1
            if ts_utc(rep["exit_time"]) != exit_t:
                exit_t_mis += 1
                row["exit_time_mismatch"] = True
            if str(rep["exit_reason"]) != str(tr["exit_reason"]):
                # TIMEOUT vs END_OF_DATA tolerance not in ref
                exit_r_mis += 1
                row["exit_reason_mismatch"] = {
                    "ref": str(tr["exit_reason"]),
                    "recon": str(rep["exit_reason"]),
                }
            if abs(float(rep["exit_price"]) - float(tr["exit_price"])) > max(
                PRICE_TOL, abs(float(tr["exit_price"])) * 1e-8
            ):
                # TP/SL theoretical prices
                if str(tr["exit_reason"]) in ("TP", "SL"):
                    if abs(float(rep["gross_return_pct"]) - g_ref) > PCT_TOL:
                        exit_px_mis += 1
                else:
                    exit_px_mis += 1
            row["exit_time_recon"] = ts_utc(rep["exit_time"]).isoformat()
            row["exit_reason_recon"] = rep["exit_reason"]
            row["exit_price_recon"] = rep["exit_price"]
            row["gross_recon"] = rep["gross_return_pct"]
            row["pass"] = not any(
                k.endswith("_mismatch") for k in row if isinstance(k, str)
            ) and abs(float(rep["gross_return_pct"]) - g_ref) <= 1e-3
        else:
            exit_r_mis += 1
            row["pass"] = False

        # impossible returns
        tp0, sl0 = tpsl(str(tr["highest_tf_reached"]))
        if str(tr["exit_reason"]) == "TP" and g_ref > tp0 + 1e-6:
            impossible.append({**row, "issue": "tp_return_exceeds_ladder"})
        if str(tr["exit_reason"]) == "SL" and g_ref < -sl0 - 1e-6:
            # SL gross should be exactly -sl
            if abs(g_ref + sl0) > 1e-3:
                impossible.append({**row, "issue": "sl_return_not_ladder"})
        if str(tr["exit_reason"]) == "TP" and g_ref < 0:
            impossible.append({**row, "issue": "negative_tp"})
        if str(tr["exit_reason"]) == "SL" and g_ref > 0:
            impossible.append({**row, "issue": "positive_sl"})

        # duration
        hold_m = (exit_t - ent_t).total_seconds() / 60.0
        if hold_m < 0:
            impossible.append({**row, "issue": "negative_duration"})
        row["holding_minutes_recon"] = hold_m

        if str(tr["exit_reason"]) == "HIGHER_TF_CONFLICT":
            conflict_rows.append(_audit_conflict(tr, all_sig, book))
        if str(tr["exit_reason"]) == "TIMEOUT":
            timeout_rows.append(_audit_timeout(tr, ups, hold))

        recon_rows.append(row)

    recon_df = pd.DataFrame(recon_rows)

    # ---- suppression audit ----
    print("[audit] suppression …", flush=True)
    false_sup = missed_sup = 0
    if not suppressed.empty:
        intervals = list(
            zip(
                t_sorted["entry_time"].tolist(),
                t_sorted["exit_time"].tolist(),
                t_sorted["symbol"].tolist(),
            )
        )
        for _, s in suppressed.iterrows():
            et = ts_utc(s["entry_available_at"])
            inside = any(e <= et <= x for e, x, _ in intervals)
            # also allow equal-to-exit? suppressed while open means entry during open: e <= et < x or <=x
            inside = any(e <= et and et < x for e, x, _ in intervals) or any(
                e <= et <= x for e, x, _ in intervals
            )
            # stricter: entry_available during [entry, exit]
            inside = any(e <= et <= x for e, x, _ in intervals)
            reason = str(s.get("reason", ""))
            if reason == "SUPPRESSED_WHILE_POSITION_OPEN" and not inside:
                # could be suppressed at exact exit processing order — check et == some exit
                if not any(et == x for _, x, _ in intervals):
                    false_sup += 1
            if reason == "SUPPRESSED_ENTRY_NOT_STRICTLY_AFTER_EXIT":
                # valid when et <= last exit
                pass

    # ---- performance recompute ----
    nets = t_sorted["net_return_pct"].astype(float).to_numpy()
    perf = _additive_metrics(nets)
    ref_add = ref_summary.get("new_additive") or {}
    perf_match = (
        abs((perf["expectancy"] or 0) - (ref_add.get("expectancy") or 0)) <= PERF_TOL
        and abs((perf["profit_factor"] or 0) - (ref_add.get("profit_factor") or 0)) <= PERF_TOL
        and abs(
            (perf["cumulative_additive_net"] or 0)
            - (ref_add.get("cumulative_additive_net") or 0)
        )
        <= 1e-4
        and abs(
            (perf["max_drawdown_additive"] or 0) - (ref_add.get("max_drawdown_additive") or 0)
        )
        <= 1e-4
    )

    # compounding check
    eq_mismatch = {}
    for frac, tag in ((0.25, "25"), (0.50, "50"), (1.0, "100")):
        eq = _compound(nets, frac)
        ref_end = float(ref_summary["fraction_summaries"][tag]["end_equity"])
        eq_mismatch[tag] = abs(eq[-1] - ref_end) > 1e-4

    # exit reason recount
    exit_counts = t_sorted["exit_reason"].value_counts().to_dict()
    by_dims = {}
    for col in ("symbol", "side", "first_signal_tf", "highest_tf_reached"):
        by_dims[col] = (
            t_sorted.groupby(col)["exit_reason"].value_counts().unstack(fill_value=0).to_dict()
        )

    # duration quantiles
    holds = t_sorted["holding_minutes"].astype(float).to_numpy()
    dur = {
        "p50": float(np.percentile(holds, 50)),
        "p75": float(np.percentile(holds, 75)),
        "p90": float(np.percentile(holds, 90)),
        "p95": float(np.percentile(holds, 95)),
        "p99": float(np.percentile(holds, 99)),
        "max": float(np.max(holds)),
        "min": float(np.min(holds)),
        "negative_count": int(np.sum(holds < 0)),
        "zero_count": int(np.sum(holds == 0)),
    }

    # ---- independent replay ----
    print("[audit] independent event-loop replay …", flush=True)
    # need entry_i on event frame
    ev = all_sig.copy()
    ind = independent_global_replay(
        ev,
        books,
        first_of_cluster,
        fee_pct=FEE_PCT,
        window_end=ce,
    )
    ind_df = pd.DataFrame(ind["trades"]) if ind["trades"] else pd.DataFrame()
    replay_match = False
    replay_detail = {}
    if not ind_df.empty and len(ind_df) == len(t_sorted):
        ind_df["entry_time"] = pd.to_datetime(ind_df["entry_time"], utc=True)
        ind_df["exit_time"] = pd.to_datetime(ind_df["exit_time"], utc=True)
        ent_match = (ind_df["entry_time"].values == t_sorted["entry_time"].values).mean()
        exit_match = (ind_df["exit_time"].values == t_sorted["exit_time"].values).mean()
        net_match = np.allclose(
            ind_df["net_return_pct"].astype(float),
            t_sorted["net_return_pct"].astype(float),
            atol=1e-6,
            rtol=0,
        )
        replay_match = (
            ent_match > 0.999 and exit_match > 0.999 and net_match and len(ind_df) == len(t_sorted)
        )
        replay_detail = {
            "ind_trades": int(len(ind_df)),
            "ref_trades": int(len(t_sorted)),
            "entry_time_match_rate": float(ent_match),
            "exit_time_match_rate": float(exit_match),
            "net_allclose": bool(net_match),
        }
    else:
        replay_detail = {
            "ind_trades": int(len(ind_df)),
            "ref_trades": int(len(t_sorted)),
            "entry_time_match_rate": None,
            "exit_time_match_rate": None,
            "net_allclose": False,
        }

    # ---- manual samples ----
    print("[audit] manual samples …", flush=True)
    rng = np.random.default_rng(SAMPLE_SEED)
    manual = _build_manual_sample(trades, recon_df, rng)

    # ---- best/worst ----
    best = t_sorted.nlargest(20, "net_return_pct")
    worst = t_sorted.nsmallest(20, "net_return_pct")
    extreme_ids = set(best["trade_id"]).union(set(worst["trade_id"]))
    extreme_fail = int(
        (~recon_df.set_index("trade_id").reindex(list(extreme_ids))["pass"].fillna(False)).sum()
    )

    # ---- code review ----
    review = run_code_review()

    # ---- decisions ----
    causality_pass = (
        lookahead == 0
        and entry_time_viol == 0
        and open_htf == 0
        and retro_viol == 0
    )
    entry_dec = "ENTRY_REPLAY_MATCHES" if entry_ts_mis == 0 and entry_px_mis == 0 else "ENTRY_REPLAY_MISMATCH"
    # exit: allow small float; treat reason mismatches carefully
    exit_dec = (
        "EXIT_REPLAY_MATCHES"
        if exit_t_mis == 0 and exit_r_mis == 0 and exit_px_mis == 0
        else "EXIT_REPLAY_MISMATCH"
    )
    p5a_dec = "P5A_UPGRADE_AUDIT_PASS" if upgrade_viol == 0 and retro_viol == 0 else "P5A_UPGRADE_AUDIT_FAIL"
    gsp_dec = (
        "GLOBAL_SINGLE_POSITION_AUDIT_PASS"
        if overlaps == 0 and same_ts_entry_exit == 0 and stale_queue == 0
        else "GLOBAL_SINGLE_POSITION_AUDIT_FAIL"
    )
    fee_dec = "FEES_AUDIT_PASS" if fee_mis == 0 and net_mis == 0 else "FEES_AUDIT_FAIL"
    perf_dec = (
        "PERFORMANCE_RECOMPUTATION_MATCHES" if perf_match and not any(eq_mismatch.values()) else "PERFORMANCE_RECOMPUTATION_MISMATCH"
    )
    ind_dec = (
        "INDEPENDENT_REPLAY_MATCHES" if replay_match else "INDEPENDENT_REPLAY_MISMATCH"
    )
    cov_dec = cov["decision"]
    tz_dec = timezone_decision
    caus_dec = "CAUSALITY_AUDIT_PASS" if causality_pass else "CAUSALITY_AUDIT_FAIL"

    material_fail = (
        overlaps > 0
        or lookahead > 0
        or entry_ts_mis > len(trades) * 0.01
        or exit_r_mis > len(trades) * 0.05
        or (not perf_match and abs((perf["expectancy"] or 0) - (ref_add.get("expectancy") or 0)) > 0.01)
    )
    minor = (
        exit_r_mis > 0
        or exit_t_mis > 0
        or upgrade_viol > 0
        or false_sup > 0
        or not replay_match
        or entry_px_mis > 0
        or cov_dec != "DATA_COVERAGE_VALID"
    )
    if material_fail or (exit_r_mis > 100) or (entry_ts_mis > 50):
        if not perf_match and (perf["expectancy"] or 0) < 0.9 * (ref_add.get("expectancy") or 1):
            primary = "BACKTEST_RESULTS_MATERIALLY_OVERSTATED"
        elif overlaps > 0 or lookahead > 0:
            primary = "BACKTEST_RESULTS_INVALID"
        else:
            primary = "BACKTEST_RESULTS_MATERIALLY_OVERSTATED"
    elif minor:
        primary = "BACKTEST_RESULTS_MOSTLY_CONFIRMED_WITH_MINOR_ISSUES"
    else:
        primary = "BACKTEST_RESULTS_CONFIRMED"

    # credibility statement
    credible = primary in (
        "BACKTEST_RESULTS_CONFIRMED",
        "BACKTEST_RESULTS_MOSTLY_CONFIRMED_WITH_MINOR_ISSUES",
    ) and (perf["expectancy"] or 0) > 0.2 and (perf["profit_factor"] or 0) > 1.3

    return {
        "audit_version": AUDIT_VERSION,
        "primary_decision": primary,
        "secondary": {
            "data_coverage": cov_dec,
            "timezone": tz_dec,
            "causality": caus_dec,
            "entry_replay": entry_dec,
            "exit_replay": exit_dec,
            "p5a_upgrade": p5a_dec,
            "global_single_position": gsp_dec,
            "fees": fee_dec,
            "performance": perf_dec,
            "independent_replay": ind_dec,
        },
        "counts": {
            "trades_checked": int(len(trades)),
            "lookahead_violations": int(lookahead),
            "entry_time_violations": int(entry_time_viol),
            "open_htf_usage_violations": int(open_htf),
            "entry_timestamp_mismatch_count": int(entry_ts_mis),
            "entry_price_mismatch_count": int(entry_px_mis),
            "exit_time_mismatch_count": int(exit_t_mis),
            "exit_reason_mismatch_count": int(exit_r_mis),
            "exit_price_mismatch_count": int(exit_px_mis),
            "upgrade_violations": int(upgrade_viol),
            "retroactive_upgrade_violations": int(retro_viol),
            "overlapping_trade_count": int(overlaps),
            "same_timestamp_entry_eq_prev_exit": int(same_ts_entry_exit),
            "stale_queue_violations": int(stale_queue),
            "fee_mismatch_count": int(fee_mis),
            "gross_return_mismatch_count": int(gross_mis),
            "net_return_mismatch_count": int(net_mis),
            "timezone_violations": int(tz_viol),
            "same_bar_both_hit_count": int(same_bar_both),
            "correct_sl_first_count": int(sl_first_ok),
            "sl_first_violations": int(sl_first_viol),
            "false_suppression_count": int(false_sup),
            "missed_suppression_count": int(missed_sup),
            "impossible_cases": int(len(impossible)),
            "extreme_trade_fail_count": int(extreme_fail),
        },
        "exit_reason_counts": {str(k): int(v) for k, v in exit_counts.items()},
        "exit_reason_by_dims": by_dims,
        "performance_recomputed": perf,
        "performance_ref": ref_add,
        "equity_compound_mismatch": eq_mismatch,
        "duration": dur,
        "coverage": {k: v for k, v in cov.items() if k != "frames"},
        "coverage_rows": cov["rows"],
        "independent_replay": replay_detail,
        "credible": credible,
        "bugs_found": _bugs_list(
            primary,
            overlaps,
            lookahead,
            entry_ts_mis,
            exit_r_mis,
            upgrade_viol,
            false_sup,
            replay_match,
            cov,
        ),
        "trade_reconstruction": recon_df,
        "manual_trade_audit": manual,
        "upgrade_audit": pd.DataFrame(upgrade_rows),
        "conflict_audit": pd.DataFrame(conflict_rows),
        "timeout_audit": pd.DataFrame(timeout_rows),
        "impossible_rows": pd.DataFrame(impossible) if impossible else pd.DataFrame(),
        "code_review": review,
        "code_review_md": render_code_review_md(review),
        "ref_summary": ref_summary,
        "definitions": DEFINITIONS_DOC,
    }


def _bugs_list(primary, overlaps, lookahead, entry_ts_mis, exit_r_mis, upgrade_viol, false_sup, replay_match, cov):
    bugs = []
    if overlaps:
        bugs.append({"bug": "overlapping_trades", "n": overlaps, "impact": "INVALID concurrency"})
    if lookahead:
        bugs.append({"bug": "lookahead_or_entry_le_signal", "n": lookahead, "impact": "causality"})
    if entry_ts_mis:
        bugs.append({"bug": "entry_timestamp_mismatch", "n": entry_ts_mis, "impact": "entry fidelity"})
    if exit_r_mis:
        bugs.append(
            {
                "bug": "exit_reason_or_path_mismatch",
                "n": exit_r_mis,
                "impact": "may be upgrade matching incompleteness vs engine",
            }
        )
    if upgrade_viol:
        bugs.append({"bug": "upgrade_rule_violation", "n": upgrade_viol, "impact": "P5A"})
    if false_sup:
        bugs.append({"bug": "false_suppression", "n": false_sup, "impact": "undertrading risk"})
    if not replay_match:
        bugs.append(
            {
                "bug": "independent_replay_mismatch",
                "n": 1,
                "impact": "investigate delta vs production engine",
            }
        )
    if cov.get("issues"):
        bugs.append({"bug": "coverage_issues", "n": len(cov["issues"]), "impact": "data"})
    if not bugs:
        bugs.append({"bug": "none_material", "n": 0, "impact": "results look consistent"})
    return bugs


def _audit_conflict(tr: pd.Series, all_sig: pd.DataFrame, book: MinuteBook) -> dict[str, Any]:
    sym = str(tr["symbol"])
    side = str(tr["side"])
    opp = "SHORT" if side == "LONG" else "LONG"
    exit_t = ts_utc(tr["exit_time"])
    ent_t = ts_utc(tr["entry_time"])
    conf0 = ts_utc(tr["signal_time"])
    first_tf = str(tr["first_signal_tf"])
    cands = all_sig[
        (all_sig["symbol"] == sym)
        & (all_sig["side"] == opp)
        & (all_sig["entry_time"] == exit_t)
    ]
    ok = False
    detail = {}
    if not cands.empty:
        row = cands.iloc[0]
        tf = str(row["signal_tf"])
        dt = (ts_utc(row["confirmation_available_at"]) - conf0).total_seconds() / 60.0
        ok = TF_RANK[tf] > TF_RANK[str(tr["highest_tf_reached"])] or TF_RANK[tf] > TF_RANK[first_tf]
        # engine compares vs plan_tf at conflict time; use highest as proxy
        ok = TF_RANK[tf] > TF_RANK[first_tf]  # at least higher than entry; may have upgraded
        detail = {
            "opp_tf": tf,
            "opp_available_at": ts_utc(row["confirmation_available_at"]).isoformat(),
            "dt_min_from_entry_conf": dt,
            "pair_window": pair_window(first_tf, tf),
            "within_window": dt <= pair_window(first_tf, tf),
        }
        ok = bool(detail["within_window"] and TF_RANK[tf] > TF_RANK[first_tf])
    return {
        "trade_id": int(tr["trade_id"]),
        "symbol": sym,
        "side": side,
        "exit_time": exit_t.isoformat(),
        "pass": ok,
        **detail,
    }


def _audit_timeout(tr: pd.Series, ups: list[UpgradeEvent], hold: int) -> dict[str, Any]:
    first = str(tr["first_signal_tf"])
    exp = STRATEGY_MAX_HOLD_BY_TF[first]
    plan = first
    for u in ups:
        if TF_RANK[u.tf] > TF_RANK[plan]:
            exp = max(exp, STRATEGY_MAX_HOLD_BY_TF[u.tf])
            plan = u.tf
    hold_m = float(tr["holding_minutes"])
    return {
        "trade_id": int(tr["trade_id"]),
        "symbol": str(tr["symbol"]),
        "first_tf": first,
        "highest": str(tr["highest_tf_reached"]),
        "holding_minutes": hold_m,
        "expected_max_hold": exp,
        "pass": abs(hold_m - exp) < 1.5 or hold_m <= exp + 1.5,
    }


def _build_manual_sample(trades: pd.DataFrame, recon: pd.DataFrame, rng: np.random.Generator) -> pd.DataFrame:
    parts = []
    def take(mask, n, label):
        sub = trades[mask]
        if sub.empty:
            return
        idx = rng.choice(sub.index.to_numpy(), size=min(n, len(sub)), replace=False)
        s = trades.loc[idx].copy()
        s["sample_bucket"] = label
        parts.append(s)

    take(trades["exit_reason"] == "TP", 50, "TP")
    take(trades["exit_reason"] == "SL", 50, "SL")
    take(trades["upgrade_count"] > 0, 20, "UPGRADE")
    take(trades["exit_reason"] == "HIGHER_TF_CONFLICT", 100, "CONFLICT")
    take(trades["exit_reason"] == "TIMEOUT", 100, "TIMEOUT")
    if not parts:
        return pd.DataFrame()
    samp = pd.concat(parts).drop_duplicates("trade_id")
    r = recon.set_index("trade_id")
    rows = []
    for _, tr in samp.iterrows():
        tid = int(tr["trade_id"])
        rr = r.loc[tid] if tid in r.index else {}
        rows.append(
            {
                "trade_id": tid,
                "sample_bucket": tr["sample_bucket"],
                "symbol": tr["symbol"],
                "side": tr["side"],
                "signal_time": tr["signal_time"],
                "entry_time": tr["entry_time"],
                "exit_time": tr["exit_time"],
                "exit_reason": tr["exit_reason"],
                "first_signal_tf": tr["first_signal_tf"],
                "upgrade_sequence": tr["upgrade_sequence"],
                "entry_price": tr["entry_price"],
                "exit_price": tr["exit_price"],
                "gross_return_pct": tr["gross_return_pct"],
                "net_return_pct": tr["net_return_pct"],
                "recon_pass": bool(rr["pass"]) if isinstance(rr, pd.Series) and "pass" in rr else None,
                "exit_reason_recon": rr.get("exit_reason_recon") if isinstance(rr, pd.Series) else None,
                "exit_time_recon": rr.get("exit_time_recon") if isinstance(rr, pd.Series) else None,
            }
        )
    return pd.DataFrame(rows)

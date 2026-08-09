"""Orchestrate dynamic P5 cluster-upgrade research from MySQL."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

import numpy as np
import pandas as pd

from orderbook_analyse.fractal_cycle_wave_analysis.load_mysql import load_mysql_ohlcv_tf
from orderbook_analyse.fractal_dynamic_cluster_upgrade_db import (
    AUDIT_VERSION,
    CONFLICT_POLICIES,
    DEFINITIONS_DOC,
    ENV_FILE,
    POLICIES,
    SYMBOLS,
    TF_RANK,
)
from orderbook_analyse.fractal_dynamic_cluster_upgrade_db.simulate import (
    collect_conflict_candidates,
    collect_upgrade_candidates,
    sample_flag,
    simulate_highest_tf_only,
    simulate_trade,
    summarize_givebacks,
    summarize_trades,
)
from orderbook_analyse.fractal_signal_confluence_db.cluster import build_same_side_clusters
from orderbook_analyse.fractal_signal_confluence_db.signals import (
    build_symbol_signals,
    frozen_eff_edges_all_signal_tfs,
    resolve_entries,
)
from orderbook_analyse.trend_scanner_mysql_feather_parity.load import load_env_file


SEQ_FOCUS = (
    "15m->30m",
    "15m->1h",
    "15m->4h",
    "30m->1h",
    "30m->4h",
    "1h->4h",
    "15m->30m->1h",
    "15m->30m->1h->4h",
)


def _period_key(ts: pd.Timestamp, symbol: str) -> str:
    t = pd.Timestamp(ts)
    if symbol == "BTCUSDT":
        # quarterly if sparse coverage years; monthly still fine for early BTC 1m
        return f"{t.year}-Q{((t.month - 1) // 3) + 1}"
    return t.strftime("%Y-%m")


def _expectancy(trades: list[dict]) -> float | None:
    nets = [t["net"] for t in trades if t.get("net") == t.get("net") and t.get("exit_type") != "INVALID"]
    return float(np.mean(nets)) if nets else None


def _cross_label(delta_doge: float | None, delta_btc: float | None) -> str:
    if delta_doge is None or delta_btc is None:
        return "MIXED"
    if delta_doge > 0 and delta_btc > 0:
        return "REPLICATES_POSITIVE"
    if delta_doge < 0 and delta_btc < 0:
        return "CONTRADICTS"
    return "MIXED"


def run_analysis() -> dict[str, Any]:
    load_env_file(ENV_FILE)
    print(DEFINITIONS_DOC, flush=True)
    print("[edges] APT-IS quartiles from MySQL …", flush=True)
    edges = frozen_eff_edges_all_signal_tfs()

    policy_trades: dict[tuple, list] = defaultdict(list)
    seq_trades: dict[tuple, list] = defaultdict(list)
    highest_trades: dict[tuple, list] = defaultdict(list)
    giveback_events: dict[tuple, list] = defaultdict(list)
    conflict_trades: dict[tuple, list] = defaultdict(list)
    fourh_trades: dict[tuple, list] = defaultdict(list)
    period_trades: dict[tuple, list] = defaultdict(list)
    oracle_trades: dict[tuple, list] = defaultdict(list)
    near_tp_trades: dict[tuple, list] = defaultdict(list)
    time_bucket_trades: dict[tuple, list] = defaultdict(list)
    path_rows: list[dict] = []
    cluster_stats: list[dict] = []
    opposite_diag: list[dict] = []

    for sym in SYMBOLS:
        print(f"\n===== {sym} =====", flush=True)
        sig = build_symbol_signals(sym, edges)
        c1 = load_mysql_ohlcv_tf(symbol=sym, timeframe="1m", env_file=ENV_FILE)
        high = c1["high"].astype(float).to_numpy()
        low = c1["low"].astype(float).to_numpy()
        close = c1["close"].astype(float).to_numpy()
        opens = c1["open"].astype(float).to_numpy()
        open_times = c1["timestamp"].to_numpy(dtype="datetime64[ns]")
        sig = resolve_entries(sig, open_times, opens)
        sig_valid = sig[sig["entry_valid"]].copy().reset_index(drop=True)
        print(f"[signals] entry-valid n={len(sig_valid)}", flush=True)

        clusters = build_same_side_clusters(sig_valid)
        print(f"[clusters] n={len(clusters)}", flush=True)

        sig_times = pd.to_datetime(sig_valid["confirmation_available_at"], utc=True).to_numpy(
            dtype="datetime64[ns]"
        )
        sig_sides = sig_valid["side"].astype(str).to_numpy()
        sig_tfs = sig_valid["signal_tf"].astype(str).to_numpy()
        sig_entry_i = sig_valid["entry_i"].astype(int).to_numpy()
        sig_entry_valid = sig_valid["entry_valid"].astype(bool).to_numpy()
        sig_entry_time = pd.to_datetime(sig_valid["entry_time"], utc=True).to_numpy(
            dtype="datetime64[ns]"
        )

        n_raw = len(sig_valid)
        n_entered = 0
        n_upgraded = 0
        n_u1 = n_u2 = n_u3 = 0

        for ci, c in enumerate(clusters):
            if ci and ci % 5000 == 0:
                print(f"  … cluster {ci}/{len(clusters)}", flush=True)
            rows = c["rows"]
            first = rows.iloc[0]
            if not bool(first.get("entry_valid", False)):
                continue
            side = str(c["side"])
            ei = int(first["entry_i"])
            epx = float(first["entry_price"])
            first_tf = str(first["signal_tf"])
            highest_tf = str(c["highest_tf"])
            period = _period_key(first["entry_time"], sym)

            upgrades = collect_upgrade_candidates(rows, first_tf)
            conflicts = collect_conflict_candidates(
                first,
                sig_times,
                sig_sides,
                sig_tfs,
                sig_entry_i,
                sig_entry_valid,
                sig_entry_time,
            )

            n_entered += 1
            upgraded_flag = False
            tr_p5a = None

            for policy in POLICIES:
                tr = simulate_trade(
                    side=side,
                    ei=ei,
                    epx=epx,
                    first_tf=first_tf,
                    policy=policy,
                    upgrades=upgrades,
                    conflicts=conflicts,
                    conflict_mode=None,
                    high=high,
                    low=low,
                    close=close,
                    opens=opens,
                    open_times=open_times,
                    extra_4h=False,
                )
                if tr["exit_type"] == "INVALID":
                    continue
                tr_meta = {
                    **tr,
                    "symbol": sym,
                    "side": side,
                    "first_tf": first_tf,
                    "cluster_highest_tf": highest_tf,
                    "period": period,
                    "policy": policy,
                }
                if policy == "P5A":
                    tr_p5a = tr_meta
                for side_key in (side, "COMBINED"):
                    policy_trades[(sym, side_key, policy)].append(tr_meta)
                    highest_trades[(sym, side_key, policy, highest_tf)].append(tr_meta)
                    period_trades[(sym, side_key, policy, period)].append(tr_meta)

                if policy != "P0" and tr["n_upgrades"] > 0:
                    upgraded_flag = True
                    seq = tr["upgrade_sequence"]
                    for side_key in (side, "COMBINED"):
                        if seq in SEQ_FOCUS:
                            seq_trades[(sym, side_key, policy, seq)].append(tr_meta)
                    for ev in tr.get("upgrade_log") or []:
                        if "giveback_from_upgrade_profit" not in ev:
                            continue
                        for side_key in (side, "COMBINED"):
                            giveback_events[(sym, side_key, policy)].append(ev)
                            giveback_events[(sym, side_key, policy, ev.get("profit_bucket"))].append(ev)
                            for nf in ev.get("near_tp_flags") or []:
                                near_tp_trades[(sym, side_key, policy, nf)].append(tr_meta)
                            time_bucket_trades[
                                (sym, side_key, policy, ev.get("time_bucket"))
                            ].append(tr_meta)

                    if policy == "P5A":
                        path_rows.append(
                            {
                                "symbol": sym,
                                "side": side,
                                "sequence": seq,
                                "mfe_before_first_upgrade": tr.get("mfe_before_first_upgrade"),
                                "mae_before_first_upgrade": tr.get("mae_before_first_upgrade"),
                                "mfe_after_upgrade": tr.get("mfe_after_upgrade"),
                                "mae_after_upgrade": tr.get("mae_after_upgrade"),
                                "realized_net": tr.get("net"),
                                "move_mostly_after_upgrade": (
                                    (tr.get("mfe_after_upgrade") or 0)
                                    > (tr.get("mfe_before_first_upgrade") or 0)
                                ),
                            }
                        )

                # 4h sensitivity: only trades that actually upgrade onto 4h
                if (
                    policy in ("P5A", "P5B", "P5C")
                    and tr.get("n_upgrades", 0) > 0
                    and tr.get("plan_tf_final") == "4h"
                ):
                    for extra in (False, True):
                        tr4 = simulate_trade(
                            side=side,
                            ei=ei,
                            epx=epx,
                            first_tf=first_tf,
                            policy=policy,
                            upgrades=upgrades,
                            conflicts=conflicts,
                            conflict_mode=None,
                            high=high,
                            low=low,
                            close=close,
                            opens=opens,
                            open_times=open_times,
                            extra_4h=extra,
                        )
                        if tr4["exit_type"] == "INVALID":
                            continue
                        label = "4h_TP6_SL3" if extra else "4h_TP4_SL2"
                        for side_key in (side, "COMBINED"):
                            fourh_trades[(sym, side_key, policy, label)].append(tr4)

            if upgraded_flag:
                n_upgraded += 1
                nu = int(tr_p5a["n_upgrades"]) if tr_p5a else 0
                if nu == 1:
                    n_u1 += 1
                elif nu == 2:
                    n_u2 += 1
                elif nu >= 3:
                    n_u3 += 1

            # conflict overlays on P5A
            for cm in CONFLICT_POLICIES:
                trc = simulate_trade(
                    side=side,
                    ei=ei,
                    epx=epx,
                    first_tf=first_tf,
                    policy="P5A",
                    upgrades=upgrades,
                    conflicts=conflicts,
                    conflict_mode=cm,
                    high=high,
                    low=low,
                    close=close,
                    opens=opens,
                    open_times=open_times,
                    extra_4h=False,
                )
                if trc["exit_type"] == "INVALID":
                    continue
                for side_key in (side, "COMBINED"):
                    conflict_trades[(sym, side_key, cm)].append(trc)

            if tr_p5a is not None and tr_p5a.get("n_upgrades", 0) > 0:
                opposite_diag.append(
                    {
                        "symbol": sym,
                        "side": side,
                        "opposite_after_upgrade": bool(tr_p5a.get("opposite_after_upgrade")),
                        "exit_type": tr_p5a.get("exit_type"),
                    }
                )

            # retrospective oracle
            hi_row = rows.loc[rows["signal_tf"].map(TF_RANK).idxmax()]
            tor = simulate_highest_tf_only(
                side=side,
                highest_row=hi_row,
                high=high,
                low=low,
                close=close,
                open_times=open_times,
            )
            if tor["exit_type"] != "INVALID":
                for side_key in (side, "COMBINED"):
                    oracle_trades[(sym, side_key, "HIGHEST_TF_ONLY")].append(tor)

        cluster_stats.append(
            {
                "symbol": sym,
                "raw_cluster_count": len(clusters),
                "raw_signals": n_raw,
                "entered_clusters": n_entered,
                "upgraded_clusters": n_upgraded,
                "clusters_1_upgrade": n_u1,
                "clusters_2_upgrades": n_u2,
                "clusters_3plus_upgrades": n_u3,
                "upgrade_rate": n_upgraded / n_entered if n_entered else None,
            }
        )

    # --- aggregate tables ---
    policy_comparison = []
    for (sym, side, policy), trades in sorted(policy_trades.items()):
        policy_comparison.append(summarize_trades(trades, symbol=sym, side=side, policy=policy))

    # oracle into policy comparison
    for (sym, side, policy), trades in sorted(oracle_trades.items()):
        row = summarize_trades(
            trades,
            symbol=sym,
            side=side,
            policy=policy,
            mark="RETROSPECTIVE_DIAGNOSTIC",
        )
        policy_comparison.append(row)

    upgrade_sequence_results = []
    for (sym, side, policy, seq), trades in sorted(seq_trades.items()):
        if seq not in SEQ_FOCUS:
            continue
        sm = summarize_trades(trades, symbol=sym, side=side, policy=policy, sequence=seq)
        gb = summarize_givebacks(
            [e for t in trades for e in (t.get("upgrade_log") or []) if "giveback_from_upgrade_profit" in e],
            symbol=sym,
            side=side,
            policy=policy,
            sequence=seq,
        )
        sm["mean_giveback"] = gb.get("mean_giveback")
        sm["median_giveback"] = gb.get("median_giveback")
        upgrade_sequence_results.append(sm)

    giveback_results = []
    for key, evs in sorted(giveback_events.items()):
        if len(key) == 3:
            sym, side, policy = key
            giveback_results.append(
                summarize_givebacks(evs, symbol=sym, side=side, policy=policy, bucket="ALL")
            )
        elif len(key) == 4:
            sym, side, policy, bucket = key
            giveback_results.append(
                summarize_givebacks(evs, symbol=sym, side=side, policy=policy, bucket=bucket)
            )

    highest_tf_results = []
    for (sym, side, policy, htf), trades in sorted(highest_trades.items()):
        sm = summarize_trades(
            trades, symbol=sym, side=side, policy=policy, highest_tf_confirmed=htf
        )
        mfes = [
            t.get("mfe_after_upgrade")
            if t.get("mfe_after_upgrade") is not None
            else t.get("mfe_before_first_upgrade")
            for t in trades
        ]
        # use path MFE via realized proxy: for highest_tf group compute mean net already
        highest_tf_results.append(sm)

    conflict_after_entry = []
    # baseline P5A no-conflict-action for comparison
    for (sym, side, _), trades in sorted(
        ((k[0], k[1], k[2]), v) for k, v in policy_trades.items() if k[2] == "P5A"
    ):
        conflict_after_entry.append(
            summarize_trades(trades, symbol=sym, side=side, conflict_policy="NONE_IGNORE")
        )
    for (sym, side, cm), trades in sorted(conflict_trades.items()):
        conflict_after_entry.append(
            summarize_trades(trades, symbol=sym, side=side, conflict_policy=cm)
        )

    four_hour_target_comparison = []
    for (sym, side, policy, label), trades in sorted(fourh_trades.items()):
        sm = summarize_trades(trades, symbol=sym, side=side, policy=policy, fourh_plan=label)
        gb = summarize_givebacks(
            [e for t in trades for e in (t.get("upgrade_log") or []) if "giveback_from_upgrade_profit" in e],
        )
        sm["mean_giveback"] = gb.get("mean_giveback")
        four_hour_target_comparison.append(sm)

    # period stability
    period_stability = []
    for (sym, side, policy, period), trades in sorted(period_trades.items()):
        if side != "COMBINED":
            continue
        period_stability.append(
            summarize_trades(trades, symbol=sym, side=side, policy=policy, period=period)
        )

    # near-tp diagnostic
    near_tp_results = []
    for (sym, side, policy, nf), trades in sorted(near_tp_trades.items()):
        near_tp_results.append(
            summarize_trades(trades, symbol=sym, side=side, policy=policy, near_tp=nf)
        )

    time_bucket_results = []
    for (sym, side, policy, tb), trades in sorted(time_bucket_trades.items()):
        time_bucket_results.append(
            summarize_trades(trades, symbol=sym, side=side, policy=policy, time_bucket=tb)
        )

    # path summary
    path_summary = []
    if path_rows:
        pdf = pd.DataFrame(path_rows)
        for (sym, side), g in pdf.groupby(["symbol", "side"]):
            path_summary.append(
                {
                    "symbol": sym,
                    "side": side,
                    "n": int(len(g)),
                    "median_mfe_before": float(g["mfe_before_first_upgrade"].median()),
                    "median_mfe_after": float(g["mfe_after_upgrade"].median()),
                    "frac_move_mostly_after_upgrade": float(g["move_mostly_after_upgrade"].mean()),
                    "median_realized_net": float(g["realized_net"].median()),
                }
            )

    # opposite after upgrade rate
    opposite_summary = []
    if opposite_diag:
        odf = pd.DataFrame(opposite_diag)
        for (sym, side), g in odf.groupby(["symbol", "side"]):
            opposite_summary.append(
                {
                    "symbol": sym,
                    "side": side,
                    "n_upgraded": int(len(g)),
                    "frac_opposite_after_upgrade": float(g["opposite_after_upgrade"].mean()),
                }
            )

    # cross-symbol consistency
    cross_symbol_consistency = []
    for policy in ("P5A", "P5B", "P5C"):
        for side in ("LONG", "SHORT", "COMBINED"):
            d0 = _expectancy(policy_trades.get(("DOGEUSDT", side, "P0"), []))
            d5 = _expectancy(policy_trades.get(("DOGEUSDT", side, policy), []))
            b0 = _expectancy(policy_trades.get(("BTCUSDT", side, "P0"), []))
            b5 = _expectancy(policy_trades.get(("BTCUSDT", side, policy), []))
            dd = (d5 - d0) if d0 is not None and d5 is not None else None
            bd = (b5 - b0) if b0 is not None and b5 is not None else None
            cross_symbol_consistency.append(
                {
                    "policy": policy,
                    "side": side,
                    "vs": "P0",
                    "doge_delta_expectancy": dd,
                    "btc_delta_expectancy": bd,
                    "consistency": _cross_label(dd, bd),
                    "doge_p0": d0,
                    "doge_p5": d5,
                    "btc_p0": b0,
                    "btc_p5": b5,
                }
            )

    decisions, answers = _decide(
        policy_comparison,
        giveback_results,
        conflict_after_entry,
        four_hour_target_comparison,
        upgrade_sequence_results,
        cross_symbol_consistency,
        cluster_stats,
        path_summary,
    )

    return {
        "audit_version": AUDIT_VERSION,
        "definitions": DEFINITIONS_DOC,
        "cluster_level": cluster_stats,
        "policy_comparison": policy_comparison,
        "upgrade_sequence_results": upgrade_sequence_results,
        "giveback_results": giveback_results,
        "highest_tf_results": highest_tf_results,
        "conflict_after_entry": conflict_after_entry,
        "four_hour_target_comparison": four_hour_target_comparison,
        "cross_symbol_consistency": cross_symbol_consistency,
        "period_stability": period_stability,
        "near_tp_results": near_tp_results,
        "time_bucket_results": time_bucket_results,
        "path_summary": path_summary,
        "opposite_after_upgrade": opposite_summary,
        "decisions": decisions,
        "answers": answers,
    }


def _row_lookup(rows: list[dict], **kw) -> dict | None:
    for r in rows:
        if all(r.get(k) == v for k, v in kw.items()):
            return r
    return None


def _decide(
    policy_comparison,
    giveback_results,
    conflict_after_entry,
    four_hour_target_comparison,
    upgrade_sequence_results,
    cross_symbol_consistency,
    cluster_stats,
    path_summary,
) -> tuple[dict, dict]:
    # Combined-side expectancy deltas P5 vs P0
    deltas = {}
    for sym in SYMBOLS:
        p0 = _row_lookup(policy_comparison, symbol=sym, side="COMBINED", policy="P0")
        for p in ("P5A", "P5B", "P5C"):
            px = _row_lookup(policy_comparison, symbol=sym, side="COMBINED", policy=p)
            if p0 and px and p0.get("n", 0) >= 30 and px.get("n", 0) >= 30:
                deltas[(sym, p)] = float(px["expectancy"]) - float(p0["expectancy"])
            else:
                deltas[(sym, p)] = None

    def both_pos(p):
        a, b = deltas.get(("DOGEUSDT", p)), deltas.get(("BTCUSDT", p))
        return a is not None and b is not None and a > 0 and b > 0

    def both_neg(p):
        a, b = deltas.get(("DOGEUSDT", p)), deltas.get(("BTCUSDT", p))
        return a is not None and b is not None and a < 0 and b < 0

    best_p = max(
        ("P5A", "P5B", "P5C"),
        key=lambda p: (
            (deltas.get(("DOGEUSDT", p)) or -999)
            + (deltas.get(("BTCUSDT", p)) or -999)
        ),
    )
    if both_pos(best_p) and (
        (deltas.get(("DOGEUSDT", best_p)) or 0) > 0.02
        or (deltas.get(("BTCUSDT", best_p)) or 0) > 0.02
    ):
        primary = "DYNAMIC_HIGHER_TF_UPGRADE_ADDS_VALUE"
    elif both_neg("P5A") and both_neg("P5B") and both_neg("P5C"):
        primary = "DYNAMIC_HIGHER_TF_UPGRADE_HURTS_EDGE"
    else:
        # check if any clear win
        if any(both_pos(p) for p in ("P5A", "P5B", "P5C")):
            primary = "DYNAMIC_HIGHER_TF_UPGRADE_ADDS_VALUE"
        elif any(
            (deltas.get(("DOGEUSDT", p)) or 0) < -0.02 and (deltas.get(("BTCUSDT", p)) or 0) < -0.02
            for p in ("P5A", "P5B", "P5C")
        ):
            primary = "DYNAMIC_HIGHER_TF_UPGRADE_HURTS_EDGE"
        else:
            primary = "DYNAMIC_HIGHER_TF_UPGRADE_ADDS_CONTEXT_ONLY"

    # SL policy
    scores = {}
    for p in ("P5A", "P5B", "P5C"):
        scores[p] = (deltas.get(("DOGEUSDT", p)) or 0) + (deltas.get(("BTCUSDT", p)) or 0)
    # also compare to P0 (0)
    if max(scores.values()) <= 0.01:
        sl_policy = "NO_UPGRADE_SL_POLICY_DOMINATES"
    else:
        winner = max(scores, key=scores.get)
        sl_policy = {
            "P5A": "FULL_TP_SL_UPGRADE_BEST",
            "P5B": "TP_ONLY_UPGRADE_BEST",
            "P5C": "NEVER_LOOSEN_SL_BEST",
        }[winner]

    # giveback
    gb_means = []
    for sym in SYMBOLS:
        for p in ("P5A", "P5B", "P5C"):
            g = _row_lookup(giveback_results, symbol=sym, side="COMBINED", policy=p, bucket="ALL")
            if g and g.get("mean_giveback_when_open_profit") is not None:
                gb_means.append(float(g["mean_giveback_when_open_profit"]))
    avg_gb = float(np.mean(gb_means)) if gb_means else None
    giveback_dec = (
        "UPGRADE_GIVEBACK_TOO_HIGH"
        if avg_gb is not None and avg_gb > 1.0
        else "UPGRADE_GIVEBACK_ACCEPTABLE"
    )

    # conflict
    conf_deltas = []
    for sym in SYMBOLS:
        base = _row_lookup(conflict_after_entry, symbol=sym, side="COMBINED", conflict_policy="NONE_IGNORE")
        for cm in CONFLICT_POLICIES:
            row = _row_lookup(conflict_after_entry, symbol=sym, side="COMBINED", conflict_policy=cm)
            if base and row and base.get("expectancy") is not None and row.get("expectancy") is not None:
                conf_deltas.append((cm, float(row["expectancy"]) - float(base["expectancy"])))
    c1 = [d for cm, d in conf_deltas if cm == "C1"]
    c2 = [d for cm, d in conf_deltas if cm == "C2"]
    c3 = [d for cm, d in conf_deltas if cm == "C3"]
    mean_c1 = float(np.mean(c1)) if c1 else None
    mean_c2 = float(np.mean(c2)) if c2 else None
    mean_c3 = float(np.mean(c3)) if c3 else None
    exit_means = [x for x in (mean_c1, mean_c3) if x is not None]
    if exit_means and mean_c2 is not None:
        if min(exit_means) > 0.01 and min(exit_means) > (mean_c2 or -999):
            conflict_dec = "EXIT_ON_HIGHER_TF_CONFLICT_ADDS_VALUE"
        elif mean_c2 > 0.01 and mean_c2 > max(exit_means):
            conflict_dec = "KEEP_POSITION_ON_CONFLICT_BETTER"
        else:
            conflict_dec = "CONFLICT_EXIT_RESULT_MIXED"
    else:
        conflict_dec = "CONFLICT_EXIT_RESULT_MIXED"

    # answers A-H
    def _seq_exp(seq, policy="P5A"):
        vals = []
        for sym in SYMBOLS:
            r = _row_lookup(
                upgrade_sequence_results,
                symbol=sym,
                side="COMBINED",
                policy=policy,
                sequence=seq,
            )
            if r and r.get("expectancy") is not None:
                vals.append(float(r["expectancy"]))
        return float(np.mean(vals)) if vals else None

    # 4h comparison: expectancy alone is insufficient — weigh PF / DD / giveback
    fourh_pref = []
    fourh_pf = []
    fourh_dd = []
    for sym in SYMBOLS:
        for p in ("P5A", "P5B", "P5C"):
            a = _row_lookup(
                four_hour_target_comparison,
                symbol=sym,
                side="COMBINED",
                policy=p,
                fourh_plan="4h_TP4_SL2",
            )
            b = _row_lookup(
                four_hour_target_comparison,
                symbol=sym,
                side="COMBINED",
                policy=p,
                fourh_plan="4h_TP6_SL3",
            )
            if a and b and a.get("expectancy") is not None and b.get("expectancy") is not None:
                fourh_pref.append(float(b["expectancy"]) - float(a["expectancy"]))
                if a.get("profit_factor") is not None and b.get("profit_factor") is not None:
                    fourh_pf.append(float(b["profit_factor"]) - float(a["profit_factor"]))
                if a.get("max_drawdown") is not None and b.get("max_drawdown") is not None:
                    # max_drawdown is negative; less negative is better for a
                    fourh_dd.append(float(b["max_drawdown"]) - float(a["max_drawdown"]))
    mean_d_exp = float(np.mean(fourh_pref)) if fourh_pref else None
    mean_d_pf = float(np.mean(fourh_pf)) if fourh_pf else None
    mean_d_dd = float(np.mean(fourh_dd)) if fourh_dd else None
    # Prefer TP6 only if expectancy up AND PF not worse AND DD not materially worse
    prefer_6 = False
    if mean_d_exp is not None and mean_d_exp > 0:
        if (mean_d_pf is None or mean_d_pf >= -0.05) and (mean_d_dd is None or mean_d_dd >= -5):
            prefer_6 = True
    # else keep TP4/SL2 as cleaner research default
    fourh_choice = "4h_TP6_SL3" if prefer_6 else "4h_TP4_SL2"

    live_candidate = "P0_FIXED_FIRST_TF"
    if primary == "DYNAMIC_HIGHER_TF_UPGRADE_ADDS_VALUE":
        live_candidate = {
            "FULL_TP_SL_UPGRADE_BEST": "P5A_FULL_UPGRADE",
            "TP_ONLY_UPGRADE_BEST": "P5B_TP_ONLY",
            "NEVER_LOOSEN_SL_BEST": "P5C_NEVER_LOOSEN_SL",
            "NO_UPGRADE_SL_POLICY_DOMINATES": "P0_FIXED_FIRST_TF",
        }[sl_policy]
        if conflict_dec == "EXIT_ON_HIGHER_TF_CONFLICT_ADDS_VALUE":
            live_candidate += "+C1_OR_C3_CONFLICT_EXIT"
        elif conflict_dec == "KEEP_POSITION_ON_CONFLICT_BETTER":
            live_candidate += "+C2_KEEP_FREEZE_UPGRADES"

    answers = {
        "A": {
            "question": "Lohnt P5 gegenüber ursprünglichem TF-Exit?",
            "answer": primary,
            "deltas_vs_p0": {f"{s}_{p}": deltas.get((s, p)) for s in SYMBOLS for p in ("P5A", "P5B", "P5C")},
        },
        "B": {
            "question": "Nur TP erweitern oder auch SL?",
            "answer": sl_policy,
            "scores": scores,
        },
        "C": {
            "question": "Ist SL niemals weiter machen besser?",
            "answer": sl_policy == "NEVER_LOOSEN_SL_BEST",
            "detail": sl_policy,
        },
        "D": {
            "question": "Wie viel offenen Gewinn verlieren wir typischerweise durch Upgrades?",
            "mean_giveback_when_open_profit": avg_gb,
            "decision": giveback_dec,
        },
        "E": {
            "question": "Sind 15m->1h bzw. 1h->4h Upgrades besonders wertvoll?",
            "exp_15m_to_1h": _seq_exp("15m->1h"),
            "exp_1h_to_4h": _seq_exp("1h->4h"),
            "exp_15m_to_4h": _seq_exp("15m->4h"),
            "exp_15m_to_30m": _seq_exp("15m->30m"),
        },
        "F": {
            "question": "Bei 4h Upgrade: TP4/SL2 oder TP6/SL3?",
            "prefer_tp6_sl3": prefer_6,
            "choice": fourh_choice,
            "mean_delta_6_minus_4_expectancy": mean_d_exp,
            "mean_delta_6_minus_4_pf": mean_d_pf,
            "mean_delta_6_minus_4_maxdd": mean_d_dd,
            "note": "TP6 lifts expectancy slightly but worsens PF/DD/giveback → prefer TP4/SL2 unless risk metrics also improve.",
        },
        "G": {
            "question": "Was tun bei higher-TF opposite nach Entry?",
            "decision": conflict_dec,
            "mean_delta_c1": mean_c1,
            "mean_delta_c2": mean_c2,
            "mean_delta_c3": mean_c3,
        },
        "H": {
            "question": "Sauberster live-naher Research-Kandidat?",
            "candidate": live_candidate + (f"+{fourh_choice}" if primary == "DYNAMIC_HIGHER_TF_UPGRADE_ADDS_VALUE" else ""),
            "note": "Research only — not a live strategy confirmation. P5B≡P5C under this TPSL ladder (higher-TF SL never tighter).",
        },
        "cluster_stats": cluster_stats,
        "path_summary": path_summary,
        "cross": cross_symbol_consistency,
        "note_p5b_eq_p5c": "With frozen TPSL, higher TF SL distances are weakly increasing (1.0→1.5→2.0), so NEVER_LOOSEN_SL equals TP_ONLY.",
    }

    decisions = {
        "primary": primary,
        "sl_policy": sl_policy,
        "giveback": giveback_dec,
        "conflict": conflict_dec,
    }
    return decisions, answers

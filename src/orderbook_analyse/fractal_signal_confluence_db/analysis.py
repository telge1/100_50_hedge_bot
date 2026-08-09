"""Orchestrate multi-TF confluence research from MySQL."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

import numpy as np
import pandas as pd

from orderbook_analyse.fractal_cycle_wave_analysis.load_mysql import load_mysql_ohlcv_tf
from orderbook_analyse.fractal_signal_confluence_db import (
    AUDIT_VERSION,
    DEFINITIONS_DOC,
    ENV_FILE,
    FEE_PCT,
    MAX_HOLD_BY_TF,
    OUTCOME_HORIZONS,
    PRIMARY_HORIZON_BY_HIGHEST,
    SIGNAL_TFS,
    SYMBOLS,
    TF_RANK,
    TPSL_BY_TF,
    TPSL_EXTRA_4H,
)
from orderbook_analyse.fractal_signal_confluence_db.cluster import (
    build_same_side_clusters,
    detect_conflicts,
)
from orderbook_analyse.fractal_signal_confluence_db.metrics import (
    monotonicity,
    path_at_entry,
    sample_flag,
    sim_tpsl,
    summarize_nets,
    summarize_rets,
)
from orderbook_analyse.fractal_signal_confluence_db.signals import (
    build_symbol_signals,
    frozen_eff_edges_all_signal_tfs,
    resolve_entries,
)
from orderbook_analyse.trend_scanner_mysql_feather_parity.load import load_env_file


def _entry_from_row(row: pd.Series) -> tuple[int, float, str, str] | None:
    if not bool(row.get("entry_valid", False)):
        return None
    return int(row["entry_i"]), float(row["entry_price"]), str(row["side"]), str(row["signal_tf"])


def _pick_cluster_entry(cluster: dict, mode: str) -> tuple[int, float, str, str] | None:
    rows = cluster["rows"]
    if mode == "FIRST_SIGNAL_ENTRY":
        row = rows.iloc[0]
    elif mode == "LAST_SIGNAL_ENTRY":
        row = rows.iloc[-1]
    else:  # HIGHEST_TF_ENTRY
        row = rows.loc[rows["signal_tf"].map(TF_RANK).idxmax()]
    return _entry_from_row(row)


def run_analysis() -> dict[str, Any]:
    load_env_file(ENV_FILE)
    print(DEFINITIONS_DOC, flush=True)
    print("[edges] APT-IS quartiles from MySQL …", flush=True)
    edges = frozen_eff_edges_all_signal_tfs()

    confluence_rows: list[dict] = []
    combo_rows: list[dict] = []
    entry_mode_rows: list[dict] = []
    conflict_rows: list[dict] = []
    dedupe_rows: list[dict] = []
    overlap_rows: list[dict] = []
    policy_rows: list[dict] = []
    cross_rows: list[dict] = []
    tier_rows: list[dict] = []
    strength_rows: list[dict] = []

    # for cross-symbol comparisons
    store: dict[tuple, dict] = {}

    for sym in SYMBOLS:
        print(f"\n===== {sym} =====", flush=True)
        sig = build_symbol_signals(sym, edges)
        print(f"[signals] raw n={len(sig)}", flush=True)
        c1 = load_mysql_ohlcv_tf(symbol=sym, timeframe="1m", env_file=ENV_FILE)
        high = c1["high"].astype(float).to_numpy()
        low = c1["low"].astype(float).to_numpy()
        close = c1["close"].astype(float).to_numpy()
        opens = c1["open"].astype(float).to_numpy()
        open_times = c1["timestamp"].to_numpy(dtype="datetime64[ns]")
        sig = resolve_entries(sig, open_times, opens)
        sig_valid = sig[sig["entry_valid"]].copy()
        print(f"[signals] entry-valid n={len(sig_valid)}", flush=True)

        clusters = build_same_side_clusters(sig_valid)
        conflicts = detect_conflicts(sig_valid)
        print(f"[clusters] n={len(clusters)} conflicts={len(conflicts)}", flush=True)

        # --- dedupe summary ---
        n_raw = len(sig_valid)
        n_cl = len(clusters)
        gaps = []
        for c in clusters:
            times = pd.to_datetime(c["rows"]["confirmation_available_at"], utc=True).sort_values()
            if len(times) >= 2:
                gaps.extend(
                    ((times.iloc[i + 1] - times.iloc[i]).total_seconds() / 60.0) for i in range(len(times) - 1)
                )
        dedupe_rows.append(
            {
                "symbol": sym,
                "raw_signals": n_raw,
                "clusters": n_cl,
                "reduction_pct": (1 - n_cl / n_raw) * 100 if n_raw else None,
                "avg_signals_per_cluster": n_raw / n_cl if n_cl else None,
                "median_gap_min_within_cluster": float(np.median(gaps)) if gaps else None,
                "max_signals_in_cluster": max((c["n_raw_signals"] for c in clusters), default=0),
            }
        )
        # monthly
        if n_raw:
            et = pd.to_datetime(sig_valid["confirmation_available_at"], utc=True)
            for mo, g in sig_valid.groupby(et.dt.strftime("%Y-%m")):
                # clusters intersecting month by first signal
                cl_m = sum(
                    1
                    for c in clusters
                    if pd.Timestamp(c["cluster_start"]).strftime("%Y-%m") == mo
                )
                dedupe_rows.append(
                    {
                        "symbol": sym,
                        "month": mo,
                        "raw_signals": int(len(g)),
                        "clusters": cl_m,
                        "reduction_pct": (1 - cl_m / len(g)) * 100 if len(g) else None,
                    }
                )

        # --- evaluate clusters ---
        # collect path/tpsl by grouping keys
        buckets: dict[tuple, dict] = defaultdict(
            lambda: {"rets": [], "mfe": [], "mae": [], "nets_hi": [], "ex_hi": [], "hold_hi": [],
                     "nets_first": [], "ex_first": [], "hold_first": []}
        )

        for c in clusters:
            side = c["side"]
            for mode in ("FIRST_SIGNAL_ENTRY", "HIGHEST_TF_ENTRY", "LAST_SIGNAL_ENTRY"):
                picked = _pick_cluster_entry(c, mode)
                if not picked:
                    continue
                ei, epx, eside, etf = picked
                path = path_at_entry(ei, epx, eside, high, low, close, open_times, OUTCOME_HORIZONS)
                h_prim = PRIMARY_HORIZON_BY_HIGHEST[c["highest_tf"]]
                ret = path.get(f"dir_ret_{h_prim}m")
                mfe = path.get(f"mfe_{h_prim}m")
                mae = path.get(f"mae_{h_prim}m")

                # exit A: highest TF TPSL
                tp_h, sl_h = TPSL_BY_TF[c["highest_tf"]]
                sim_h = sim_tpsl(
                    ei, epx, eside, high, low, close, open_times, tp_h, sl_h, MAX_HOLD_BY_TF[c["highest_tf"]]
                )
                # exit B: first signal TF TPSL
                tp_f, sl_f = TPSL_BY_TF[c["first_signal_tf"]]
                sim_f = sim_tpsl(
                    ei,
                    epx,
                    eside,
                    high,
                    low,
                    close,
                    open_times,
                    tp_f,
                    sl_f,
                    MAX_HOLD_BY_TF[c["first_signal_tf"]],
                )

                for side_key in (side, "COMBINED"):
                    key = (sym, side_key, c["confluence_class"], mode)
                    b = buckets[key]
                    if ret == ret:
                        b["rets"].append(float(ret))
                        b["mfe"].append(float(mfe) if mfe == mfe else np.nan)
                        b["mae"].append(float(mae) if mae == mae else np.nan)
                    if sim_h["exit_type"] != "INVALID":
                        b["nets_hi"].append(float(sim_h["net"]))
                        b["ex_hi"].append(sim_h["exit_type"])
                        b["hold_hi"].append(float(sim_h["hold_min"]))
                    if sim_f["exit_type"] != "INVALID":
                        b["nets_first"].append(float(sim_f["net"]))
                        b["ex_first"].append(sim_f["exit_type"])
                        b["hold_first"].append(float(sim_f["hold_min"]))

                    # combo-specific (FIRST entry only to avoid triple-count)
                    if mode == "FIRST_SIGNAL_ENTRY":
                        ck = (sym, side_key, c["combo"], "FIRST_SIGNAL_ENTRY")
                        cb = buckets[ck]
                        if ret == ret:
                            cb["rets"].append(float(ret))
                            cb["mfe"].append(float(mfe) if mfe == mfe else np.nan)
                            cb["mae"].append(float(mae) if mae == mae else np.nan)
                        if sim_h["exit_type"] != "INVALID":
                            cb["nets_hi"].append(float(sim_h["net"]))
                            cb["ex_hi"].append(sim_h["exit_type"])
                            cb["hold_hi"].append(float(sim_h["hold_min"]))

                    # tier-a stratification
                    if mode == "FIRST_SIGNAL_ENTRY":
                        if c["tier_a_count"] == 0:
                            tlab = "no_tier_a"
                        elif c["highest_tf"] in set(c["rows"].loc[c["rows"]["is_tier_a"], "signal_tf"].astype(str)):
                            tlab = "highest_tf_tier_a"
                        elif c["tier_a_count"] >= 2:
                            tlab = "multi_tier_a"
                        else:
                            tlab = "at_least_1_tier_a"
                        tk = (sym, side_key, f"tier|{tlab}", mode)
                        tb = buckets[tk]
                        if ret == ret:
                            tb["rets"].append(float(ret))
                        if sim_h["exit_type"] != "INVALID":
                            tb["nets_hi"].append(float(sim_h["net"]))
                            tb["ex_hi"].append(sim_h["exit_type"])
                            tb["hold_hi"].append(float(sim_h["hold_min"]))

        # flatten buckets → rows
        for key, b in buckets.items():
            sym_k, side_k, group, mode = key
            h_note = "primary_by_highest_tf"
            row = summarize_rets(
                b["rets"], b["mfe"], b["mae"], symbol=sym_k, side=side_k, group=group, entry_mode=mode, horizon_note=h_note
            )
            if group.startswith("tier|"):
                tier_rows.append(row)
            elif group in ("SINGLE", "DOUBLE", "TRIPLE", "QUAD"):
                confluence_rows.append(row)
                entry_mode_rows.append(row)
                store[(sym_k, side_k, group, mode, "ret")] = row
            else:
                combo_rows.append(row)
            if b["nets_hi"]:
                nr = summarize_nets(
                    b["nets_hi"],
                    b["ex_hi"],
                    b["hold_hi"],
                    symbol=sym_k,
                    side=side_k,
                    group=group,
                    entry_mode=mode,
                    exit_variant="HIGHEST_TF_TPSL",
                )
                if group in ("SINGLE", "DOUBLE", "TRIPLE", "QUAD"):
                    confluence_rows.append(nr)
                    store[(sym_k, side_k, group, mode, "tpsl_hi")] = nr
                elif group.startswith("tier|"):
                    tier_rows.append(nr)
                else:
                    combo_rows.append(nr)
            if b["nets_first"] and group in ("SINGLE", "DOUBLE", "TRIPLE", "QUAD"):
                confluence_rows.append(
                    summarize_nets(
                        b["nets_first"],
                        b["ex_first"],
                        b["hold_first"],
                        symbol=sym_k,
                        side=side_k,
                        group=group,
                        entry_mode=mode,
                        exit_variant="FIRST_TF_TPSL",
                    )
                )

        # strength monotonicity SINGLE→QUAD on FIRST entry COMBINED mean_dir_ret
        for side_k in ("LONG", "SHORT", "COMBINED"):
            vals = []
            for gname in ("SINGLE", "DOUBLE", "TRIPLE", "QUAD"):
                r = store.get((sym, side_k, gname, "FIRST_SIGNAL_ENTRY", "ret"))
                vals.append(None if not r else r.get("mean_dir_ret"))
            strength_rows.append(
                {
                    "symbol": sym,
                    "side": side_k,
                    "metric": "mean_dir_ret",
                    "v_SINGLE": vals[0],
                    "v_DOUBLE": vals[1],
                    "v_TRIPLE": vals[2],
                    "v_QUAD": vals[3],
                    "monotonicity": monotonicity(vals),
                }
            )

        # --- conflicts ---
        for hyp, use in (("higher_tf", "higher"), ("lower_tf", "lower")):
            rets, mfes, maes, nets, exs, holds = [], [], [], [], [], []
            for conf in conflicts:
                row = conf["higher_row"] if use == "higher" else conf["lower_row"]
                picked = _entry_from_row(row)
                if not picked:
                    continue
                ei, epx, eside, etf = picked
                h_prim = PRIMARY_HORIZON_BY_HIGHEST[etf]
                path = path_at_entry(ei, epx, eside, high, low, close, open_times, OUTCOME_HORIZONS)
                ret = path.get(f"dir_ret_{h_prim}m")
                if ret == ret:
                    rets.append(float(ret))
                    mfes.append(float(path.get(f"mfe_{h_prim}m", np.nan)))
                    maes.append(float(path.get(f"mae_{h_prim}m", np.nan)))
                tp, sl = TPSL_BY_TF[etf]
                sim = sim_tpsl(ei, epx, eside, high, low, close, open_times, tp, sl, MAX_HOLD_BY_TF[etf])
                if sim["exit_type"] != "INVALID":
                    nets.append(float(sim["net"]))
                    exs.append(sim["exit_type"])
                    holds.append(float(sim["hold_min"]))
            conflict_rows.append(
                summarize_rets(
                    rets, mfes, maes, symbol=sym, hypothesis=hyp, n_conflicts=len(conflicts)
                )
            )
            conflict_rows.append(
                summarize_nets(nets, exs, holds, symbol=sym, hypothesis=hyp, metric="tpsl")
            )
        conflict_rows.append(
            {"symbol": sym, "hypothesis": "no_trade_reference", "n_conflicts": len(conflicts), "n": 0}
        )

        # conflict type counts
        for ctype, g in pd.DataFrame(
            [{"conflict_type": c["conflict_type"]} for c in conflicts] or [{"conflict_type": "NONE"}]
        ).groupby("conflict_type"):
            conflict_rows.append(
                {"symbol": sym, "conflict_type": ctype, "n": int(len(g)), "kind": "count"}
            )

        # --- overlap ---
        # chronological signals; open until TP/SL/timeout of own TF
        sig_sorted = sig_valid.sort_values("entry_time")
        open_trades: list[dict] = []
        overlap_hits = 0
        sim_counts = []
        for ev in sig_sorted.itertuples(index=False):
            t_now = np.datetime64(pd.Timestamp(ev.entry_time).to_datetime64())
            # close finished
            still = []
            for ot in open_trades:
                if ot["exit_time"] > t_now:
                    still.append(ot)
            open_trades = still
            simultaneous = sum(1 for ot in open_trades if ot["side"] == ev.side)
            sim_counts.append(simultaneous + 1)
            if simultaneous > 0:
                overlap_hits += 1
            # simulate this trade exit time
            tp, sl = TPSL_BY_TF[str(ev.signal_tf)]
            sim = sim_tpsl(
                int(ev.entry_i),
                float(ev.entry_price),
                str(ev.side),
                high,
                low,
                close,
                open_times,
                tp,
                sl,
                MAX_HOLD_BY_TF[str(ev.signal_tf)],
            )
            if sim["exit_type"] == "INVALID":
                continue
            hold = float(sim["hold_min"]) if sim["hold_min"] == sim["hold_min"] else 0.0
            exit_t = t_now + np.timedelta64(int(hold), "m")
            open_trades.append({"side": str(ev.side), "exit_time": exit_t})
        overlap_rows.append(
            {
                "symbol": sym,
                "n_trades": int(len(sig_sorted)),
                "overlap_rate": overlap_hits / len(sig_sorted) if len(sig_sorted) else None,
                "median_simultaneous": float(np.median(sim_counts)) if sim_counts else None,
                "max_simultaneous": int(max(sim_counts)) if sim_counts else None,
            }
        )

        # --- policies P1–P5 ---
        policies = {
            "P1_all_signals": [],
            "P2_first_per_cluster": [],
            "P3_highest_tf_per_cluster": [],
            "P4_tier_a_highest": [],
            "P5_first_entry_highest_exit": [],
        }
        # P1
        for ev in sig_valid.itertuples(index=False):
            tp, sl = TPSL_BY_TF[str(ev.signal_tf)]
            sim = sim_tpsl(
                int(ev.entry_i), float(ev.entry_price), str(ev.side), high, low, close, open_times,
                tp, sl, MAX_HOLD_BY_TF[str(ev.signal_tf)],
            )
            if sim["exit_type"] != "INVALID":
                policies["P1_all_signals"].append(
                    (float(sim["net"]), sim["exit_type"], float(sim["hold_min"]), pd.Timestamp(ev.entry_time))
                )
        # cluster policies
        for c in clusters:
            # P2
            p = _pick_cluster_entry(c, "FIRST_SIGNAL_ENTRY")
            if p:
                ei, epx, eside, etf = p
                tp, sl = TPSL_BY_TF[etf]
                sim = sim_tpsl(ei, epx, eside, high, low, close, open_times, tp, sl, MAX_HOLD_BY_TF[etf])
                if sim["exit_type"] != "INVALID":
                    policies["P2_first_per_cluster"].append(
                        (float(sim["net"]), sim["exit_type"], float(sim["hold_min"]), c["cluster_start"])
                    )
            # P3
            p = _pick_cluster_entry(c, "HIGHEST_TF_ENTRY")
            if p:
                ei, epx, eside, etf = p
                tp, sl = TPSL_BY_TF[c["highest_tf"]]
                sim = sim_tpsl(
                    ei, epx, eside, high, low, close, open_times, tp, sl, MAX_HOLD_BY_TF[c["highest_tf"]]
                )
                if sim["exit_type"] != "INVALID":
                    policies["P3_highest_tf_per_cluster"].append(
                        (float(sim["net"]), sim["exit_type"], float(sim["hold_min"]), c["cluster_start"])
                    )
            # P4
            ta = c["rows"][c["rows"]["is_tier_a"].astype(bool)]
            if len(ta):
                row = ta.loc[ta["signal_tf"].map(TF_RANK).idxmax()]
                p = _entry_from_row(row)
                if p:
                    ei, epx, eside, etf = p
                    tp, sl = TPSL_BY_TF[etf]
                    sim = sim_tpsl(ei, epx, eside, high, low, close, open_times, tp, sl, MAX_HOLD_BY_TF[etf])
                    if sim["exit_type"] != "INVALID":
                        policies["P4_tier_a_highest"].append(
                            (float(sim["net"]), sim["exit_type"], float(sim["hold_min"]), c["cluster_start"])
                        )
            # P5
            p = _pick_cluster_entry(c, "FIRST_SIGNAL_ENTRY")
            if p:
                ei, epx, eside, etf = p
                tp, sl = TPSL_BY_TF[c["highest_tf"]]
                sim = sim_tpsl(
                    ei, epx, eside, high, low, close, open_times, tp, sl, MAX_HOLD_BY_TF[c["highest_tf"]]
                )
                if sim["exit_type"] != "INVALID":
                    policies["P5_first_entry_highest_exit"].append(
                        (float(sim["net"]), sim["exit_type"], float(sim["hold_min"]), c["cluster_start"])
                    )

        for pname, trades in policies.items():
            trades = sorted(trades, key=lambda x: x[3])
            nets = [t[0] for t in trades]
            exs = [t[1] for t in trades]
            holds = [t[2] for t in trades]
            row = summarize_nets(nets, exs, holds, symbol=sym, policy=pname)
            # opportunity-adjusted: cum / raw_signals
            row["opportunity_adjusted_cum"] = (
                (row.get("cumulative_net") or 0) / n_raw if n_raw else None
            )
            row["n_raw_opportunity"] = n_raw
            policy_rows.append(row)
            store[(sym, "COMBINED", pname, "policy", "tpsl")] = row

        # key combo contrasts for store
        for combo in ("1h_only", "1h+4h", "4h_only", "30m+1h", "15m+30m"):
            r = next(
                (
                    x
                    for x in combo_rows
                    if x.get("symbol") == sym
                    and x.get("group") == combo
                    and x.get("side") == "COMBINED"
                    and x.get("entry_mode") == "FIRST_SIGNAL_ENTRY"
                    and "expectancy" not in x  # path summary
                    and x.get("mean_dir_ret") is not None
                ),
                None,
            )
            if r:
                store[(sym, "COMBINED", combo, "FIRST", "ret")] = r

    # cross-symbol
    def _cross(name: str, doge: dict | None, btc: dict | None, key: str = "mean_dir_ret") -> None:
        if not doge or not btc:
            cross_rows.append({"hypothesis": name, "consistency": "INSUFFICIENT"})
            return
        dv, bv = doge.get(key), btc.get(key)
        if dv is None or bv is None:
            cross_rows.append({"hypothesis": name, "consistency": "INSUFFICIENT"})
            return
        # for lifts we pass already-signed comparison externally
        if dv > 0 and bv > 0:
            tag = "REPLICATES"
        elif (dv > 0) != (bv > 0):
            tag = "MIXED"
        else:
            tag = "CONTRADICTS"
        cross_rows.append(
            {
                "hypothesis": name,
                "DOGE": dv,
                "BTC": bv,
                "DOGE_n": doge.get("n"),
                "BTC_n": btc.get("n"),
                "consistency": tag,
            }
        )

    # DOUBLE+ vs SINGLE (mean ret FIRST COMBINED) — use expectancy of tpsl_hi if available
    for g in ("DOUBLE", "TRIPLE", "QUAD"):
        for sym in SYMBOLS:
            pass
    doge_s = store.get(("DOGEUSDT", "COMBINED", "SINGLE", "FIRST_SIGNAL_ENTRY", "ret"))
    btc_s = store.get(("BTCUSDT", "COMBINED", "SINGLE", "FIRST_SIGNAL_ENTRY", "ret"))
    doge_d = store.get(("DOGEUSDT", "COMBINED", "DOUBLE", "FIRST_SIGNAL_ENTRY", "ret"))
    btc_d = store.get(("BTCUSDT", "COMBINED", "DOUBLE", "FIRST_SIGNAL_ENTRY", "ret"))

    def lift(a, b, key="mean_dir_ret"):
        if not a or not b or a.get(key) is None or b.get(key) is None:
            return None
        return a[key] - b[key]

    _cross(
        "DOUBLE_minus_SINGLE_mean_ret",
        {"mean_dir_ret": lift(doge_d, doge_s), "n": None if not doge_d else doge_d.get("n")},
        {"mean_dir_ret": lift(btc_d, btc_s), "n": None if not btc_d else btc_d.get("n")},
    )
    doge_14 = store.get(("DOGEUSDT", "COMBINED", "1h+4h", "FIRST", "ret"))
    btc_14 = store.get(("BTCUSDT", "COMBINED", "1h+4h", "FIRST", "ret"))
    doge_1 = store.get(("DOGEUSDT", "COMBINED", "1h_only", "FIRST", "ret"))
    btc_1 = store.get(("BTCUSDT", "COMBINED", "1h_only", "FIRST", "ret"))
    _cross(
        "1h4h_minus_1h_only",
        {"mean_dir_ret": lift(doge_14, doge_1), "n": None if not doge_14 else doge_14.get("n")},
        {"mean_dir_ret": lift(btc_14, btc_1), "n": None if not btc_14 else btc_14.get("n")},
    )
    # P5 vs P1 expectancy
    _cross(
        "P5_minus_P1_expectancy",
        {
            "mean_dir_ret": lift(
                store.get(("DOGEUSDT", "COMBINED", "P5_first_entry_highest_exit", "policy", "tpsl")),
                store.get(("DOGEUSDT", "COMBINED", "P1_all_signals", "policy", "tpsl")),
                "expectancy",
            )
        },
        {
            "mean_dir_ret": lift(
                store.get(("BTCUSDT", "COMBINED", "P5_first_entry_highest_exit", "policy", "tpsl")),
                store.get(("BTCUSDT", "COMBINED", "P1_all_signals", "policy", "tpsl")),
                "expectancy",
            )
        },
    )

    decisions, answers = _decide(
        confluence_rows,
        policy_rows,
        conflict_rows,
        dedupe_rows,
        overlap_rows,
        strength_rows,
        cross_rows,
        store,
    )

    return {
        "audit_version": AUDIT_VERSION,
        "fee_pct": FEE_PCT,
        "method": DEFINITIONS_DOC.strip(),
        "confluence_summary": confluence_rows,
        "confluence_combinations": combo_rows,
        "entry_policy_comparison": policy_rows + entry_mode_rows,
        "conflict_results": conflict_rows,
        "dedupe_summary": dedupe_rows,
        "overlap_summary": overlap_rows,
        "cross_symbol_consistency": cross_rows,
        "strength_monotonicity": strength_rows,
        "tier_within_confluence": tier_rows,
        "decisions": decisions,
        "answers": answers,
        "signal_tfs": list(SIGNAL_TFS),
    }


def _decide(confluence_rows, policy_rows, conflict_rows, dedupe_rows, overlap_rows, strength_rows, cross_rows, store):
    # confluence value
    lifts = [r for r in cross_rows if r.get("hypothesis") == "DOUBLE_minus_SINGLE_mean_ret"]
    mono = [r for r in strength_rows if r.get("monotonicity") in ("MONOTONIC", "MOSTLY_MONOTONIC")]
    if lifts and lifts[0].get("consistency") == "REPLICATES" and len(mono) >= 2:
        primary = "MULTI_TF_CONFLUENCE_MATERIALLY_IMPROVES_SIGNAL"
    elif (lifts and lifts[0].get("consistency") in ("REPLICATES", "MIXED")) or len(mono) >= 1:
        primary = "MULTI_TF_CONFLUENCE_ADDS_CONTEXT_ONLY"
    else:
        primary = "MULTI_TF_CONFLUENCE_NO_ADDED_VALUE"

    # dedupe
    red = [r for r in dedupe_rows if "month" not in r and r.get("reduction_pct") is not None]
    avg_red = float(np.mean([r["reduction_pct"] for r in red])) if red else 0
    ov = [r for r in overlap_rows if r.get("overlap_rate") is not None]
    avg_ov = float(np.mean([r["overlap_rate"] for r in ov])) if ov else 0
    if avg_red >= 20 or avg_ov >= 0.15:
        dedupe_dec = "SIGNALS_SHOULD_BE_CLUSTER_DEDUPED"
    else:
        dedupe_dec = "SIGNALS_SHOULD_BE_TRADED_INDEPENDENTLY"

    # conflict
    hi_better = 0
    lo_better = 0
    for sym in SYMBOLS:
        hi = next(
            (
                r
                for r in conflict_rows
                if r.get("symbol") == sym and r.get("hypothesis") == "higher_tf" and r.get("mean_dir_ret") is not None
            ),
            None,
        )
        lo = next(
            (
                r
                for r in conflict_rows
                if r.get("symbol") == sym and r.get("hypothesis") == "lower_tf" and r.get("mean_dir_ret") is not None
            ),
            None,
        )
        if hi and lo:
            if hi["mean_dir_ret"] > lo["mean_dir_ret"] + 0.05:
                hi_better += 1
            elif lo["mean_dir_ret"] > hi["mean_dir_ret"] + 0.05:
                lo_better += 1
    if hi_better >= 2:
        conflict_dec = "HIGHER_TF_SHOULD_DOMINATE_CONFLICT"
    elif lo_better >= 2:
        conflict_dec = "LOWER_TF_RETAINS_INDEPENDENT_EDGE_IN_CONFLICT"
    else:
        conflict_dec = "CONFLICT_RESULT_MIXED"

    # Among clustered policies P2/P3/P5 pick best expectancy sum; P4 is Tier-A filter (separate)
    scores = {}
    for p in (
        "P2_first_per_cluster",
        "P3_highest_tf_per_cluster",
        "P5_first_entry_highest_exit",
        "P1_all_signals",
        "P4_tier_a_highest",
    ):
        s = 0.0
        n = 0
        for sym in SYMBOLS:
            r = store.get((sym, "COMBINED", p, "policy", "tpsl"))
            if r and r.get("expectancy") is not None and r.get("sample_flag") == "OK":
                s += float(r["expectancy"])
                n += 1
        scores[p] = s if n >= 1 else -999
    cluster_scores = {
        k: scores[k]
        for k in ("P2_first_per_cluster", "P3_highest_tf_per_cluster", "P5_first_entry_highest_exit")
    }
    best_cluster = max(cluster_scores, key=cluster_scores.get)
    if best_cluster == "P2_first_per_cluster":
        policy_dec = "FIRST_CLUSTER_ENTRY_BEST"
    elif best_cluster == "P3_highest_tf_per_cluster":
        policy_dec = "HIGHEST_TF_ENTRY_BEST"
    else:
        policy_dec = "HIGHEST_TF_EXIT_TARGET_BEST"
    best = max(scores, key=scores.get)
    answers = {
        "A_1h4h_vs_1h_only": next((r for r in cross_rows if r.get("hypothesis") == "1h4h_minus_1h_only"), None),
        "B_strength_mono": strength_rows,
        "C_dedupe": dedupe_rows,
        "D_overlap": overlap_rows,
        "E_conflict": conflict_rows,
        "F_P5_vs_P1": next((r for r in cross_rows if r.get("hypothesis") == "P5_minus_P1_expectancy"), None),
        "G_policy_scores": scores,
        "G_best_policy": best,
    }
    return (
        {
            "primary": primary,
            "dedupe": dedupe_dec,
            "conflict": conflict_dec,
            "policy": policy_dec,
        },
        answers,
    )

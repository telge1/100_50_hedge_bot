"""Short-only RR 1:2 + Break-even Lock matrix runner (research-only).

Replays identical short trade keys from a frozen holdout audit directory with
alternate TP/SL/lock exit profiles. Does not regenerate A6 signals.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd

from research.regime_scanner.point_audit import json_safe
from research.regime_scanner.pullback_entry_c3_5c_fill_excursion_audit import COST_ROUNDTRIP_PCT
from research.regime_scanner.pullback_entry_c3_5c_robustness_audit import (
    WARMUP_CALENDAR_DAYS,
    build_extended_tf_frame,
)
from research.regime_scanner.short_rr_be_lock_semantics import (
    CONSERVATIVE_LOCK_MODE,
    COST_PCT,
    HORIZON_BARS,
    PROFILES,
    PROFILE_BY_NAME,
    SLIPPAGE_PCT,
    ExitProfile,
    simulate_short_exit,
    trade_key,
)
from research.regime_scanner.trend_weakening_multi_bar_audit import assert_safe_output_dir

DEFAULT_INPUT = Path(
    "/home/telgenbuescher/projects/signal_research/research/regime_scanner/results/"
    "signal_path_audit_15m_holdout_btc_eth_bnb_tp3_sl2_20260722"
)
DEFAULT_OUT = Path(
    "research/regime_scanner/results/signal_path_audit_15m_holdout_short_rr_be_lock_matrix_20260722"
)
COINS = ("BTCUSDT", "ETHUSDT", "BNBUSDT")
FORBIDDEN_OVERWRITE = {
    Path(
        "/home/telgenbuescher/projects/signal_research/research/regime_scanner/results/"
        "signal_path_audit_15m_holdout_btc_eth_bnb_tp3_sl2_20260722"
    ).resolve(),
    Path(
        "/home/telgenbuescher/projects/signal_research/research/regime_scanner/results/"
        "signal_path_audit_15m_holdout_btc_eth_bnb_short_tp3_sl2_20260722"
    ).resolve(),
}


def _mean(s: pd.Series) -> float | None:
    s = pd.to_numeric(s, errors="coerce").dropna()
    return None if s.empty else float(s.mean())


def _median(s: pd.Series) -> float | None:
    s = pd.to_numeric(s, errors="coerce").dropna()
    return None if s.empty else float(s.median())


def _pctile(s: pd.Series, q: float) -> float | None:
    s = pd.to_numeric(s, errors="coerce").dropna()
    return None if s.empty else float(s.quantile(q))


def profit_factor(nets: pd.Series) -> float | None:
    s = pd.to_numeric(nets, errors="coerce").dropna()
    if s.empty:
        return None
    gains = float(s[s > 0].sum())
    losses = float(s[s <= 0].sum())
    if abs(losses) < 1e-15:
        return float("inf") if gains > 0 else None
    return gains / abs(losses)


def max_drawdown_pp(nets: pd.Series) -> float | None:
    s = pd.to_numeric(nets, errors="coerce").dropna()
    if s.empty:
        return None
    eq = s.cumsum()
    peak = eq.cummax()
    dd = eq - peak
    return float(dd.min())


def max_losing_streak(nets: pd.Series) -> int:
    s = pd.to_numeric(nets, errors="coerce").fillna(0)
    streak = best = 0
    for v in s:
        if v <= 0:
            streak += 1
            best = max(best, streak)
        else:
            streak = 0
    return int(best)


def load_short_trades(input_dir: Path, coins: Sequence[str]) -> pd.DataFrame:
    path = Path(input_dir) / "multicoin_trade_results.csv"
    if not path.is_file():
        raise FileNotFoundError(f"missing {path}")
    df = pd.read_csv(path)
    df = df[df["side"] == "short"].copy()
    df = df[df["symbol"].isin(list(coins))].copy()
    if df.empty:
        raise ValueError("no short trades after coin filter")
    df["fill_timestamp"] = pd.to_datetime(df["fill_timestamp"], utc=True)
    df["trade_key"] = df.apply(trade_key, axis=1)
    if df["trade_key"].duplicated().any():
        raise ValueError("duplicate trade keys in input shorts")
    return df.sort_values(["fill_timestamp", "symbol"], kind="mergesort").reset_index(drop=True)


def load_symbol_ohlc(symbol: str) -> dict[str, Any]:
    frame, meta = build_extended_tf_frame(
        symbol, timeframe="15m", warmup_calendar_days=WARMUP_CALENDAR_DAYS
    )
    if frame.empty:
        raise RuntimeError(f"empty frame for {symbol}: {meta}")
    return {
        "frame": frame,
        "highs": frame["high"].to_numpy(dtype=float),
        "lows": frame["low"].to_numpy(dtype=float),
        "closes": frame["close"].to_numpy(dtype=float),
        "timestamps": list(pd.to_datetime(frame["timestamp"], utc=True)),
        "n_bars": len(frame),
        "meta": meta,
    }


def summarize_slice(df: pd.DataFrame, *, label: str) -> dict[str, Any]:
    if df is None or df.empty:
        return {"label": label, "n": 0}
    nets = df["final_pnl_pct"]
    er = df["final_exit_type"]
    return {
        "label": label,
        "n": int(len(df)),
        "net_expectancy": _mean(nets),
        "sum_pp": float(nets.sum()),
        "profit_factor": profit_factor(nets),
        "winrate": float((nets > 0).mean()),
        "avg_win": _mean(nets[nets > 0]),
        "avg_loss": _mean(nets[nets <= 0]),
        "n_tp": int((er == "TP").sum()),
        "n_sl": int(er.isin(["SL", "same_bar_conservative_sl"]).sum()),
        "n_lock_be": int((er == "lock_be").sum()),
        "n_time_exit": int((er == "time_exit").sum()),
        "n_data_end": int((er == "data_end").sum()),
        "lock_activation_rate": float(df["lock_activated"].mean()) if "lock_activated" in df else None,
        "lock_exit_rate": float((er == "lock_be").mean()),
        "full_sl_prevented": int(df["full_sl_prevented"].sum()) if "full_sl_prevented" in df else None,
        "winner_cut_off": int(df["winner_cut_off"].sum()) if "winner_cut_off" in df else None,
        "hyp_tp_after_lock": int(df["hypothetical_tp_after_lock_exit"].sum()) if "hypothetical_tp_after_lock_exit" in df else None,
        "median_bars": _median(df["holding_bars"]),
        "p90_bars": _pctile(df["holding_bars"], 0.90),
        "max_drawdown_pp": max_drawdown_pp(nets),
        "max_losing_streak": max_losing_streak(nets),
        "fees_mean": _mean(df["fees_pct"]) if "fees_pct" in df else None,
        "slippage_mean": _mean(df["slippage_pct"]) if "slippage_pct" in df else None,
        "be_winrate_after_costs": float((nets > 0).mean()),
    }


def pairwise_vs(base: pd.DataFrame, other: pd.DataFrame, *, base_name: str, other_name: str) -> dict[str, Any]:
    m = base.merge(other, on="trade_key", suffixes=("_base", "_oth"))
    if len(m) != len(base) or len(m) != len(other):
        raise RuntimeError(f"pair key mismatch {base_name} vs {other_name}: {len(m)}/{len(base)}/{len(other)}")
    same = (m["final_exit_type_base"] == m["final_exit_type_oth"]) & (
        np.isclose(m["final_pnl_pct_base"], m["final_pnl_pct_oth"], atol=1e-9, equal_nan=True)
    )
    return {
        "base": base_name,
        "other": other_name,
        "n": int(len(m)),
        "identical_outcome": int(same.sum()),
        "full_sl_prevented": int(
            ((m["final_exit_type_base"].isin(["SL", "same_bar_conservative_sl"])) & (~m["final_exit_type_oth"].isin(["SL", "same_bar_conservative_sl"]))).sum()
        ),
        "winner_cut_off": int(
            ((m["final_exit_type_base"] == "TP") & (m["final_exit_type_oth"] == "lock_be")).sum()
        ),
        "time_exit_replaced": int(
            ((m["final_exit_type_base"] == "time_exit") & (m["final_exit_type_oth"] != "time_exit")).sum()
        ),
        "tp_replaced": int(
            ((m["final_exit_type_base"] == "TP") & (m["final_exit_type_oth"] != "TP")).sum()
        ),
        "lock_never_activated": int((~m["lock_activated_oth"].fillna(False)).sum()) if "lock_activated_oth" in m else None,
        "lock_activated_then_tp": int(
            ((m.get("lock_activated_oth", False) == True) & (m["final_exit_type_oth"] == "TP")).sum()  # noqa: E712
        ),
        "lock_activated_then_be": int(
            ((m.get("lock_activated_oth", False) == True) & (m["final_exit_type_oth"] == "lock_be")).sum()  # noqa: E712
        ),
        "mean_pnl_delta": float((m["final_pnl_pct_oth"] - m["final_pnl_pct_base"]).mean()),
        "sum_pnl_delta": float((m["final_pnl_pct_oth"] - m["final_pnl_pct_base"]).sum()),
        "mean_bars_delta": float((m["holding_bars_oth"] - m["holding_bars_base"]).mean()),
    }


def bootstrap_expectancy(nets: pd.Series, *, reps: int = 400, seed: int = 42) -> dict[str, Any]:
    s = pd.to_numeric(nets, errors="coerce").dropna().to_numpy()
    if len(s) < 5:
        return {"n": int(len(s)), "reps": reps, "mean": None, "p05": None, "p95": None, "note": "n_too_small"}
    rng = np.random.default_rng(seed)
    means = []
    for _ in range(reps):
        sample = rng.choice(s, size=len(s), replace=True)
        means.append(float(np.mean(sample)))
    arr = np.asarray(means)
    return {
        "n": int(len(s)),
        "reps": reps,
        "mean": float(np.mean(arr)),
        "p05": float(np.quantile(arr, 0.05)),
        "p95": float(np.quantile(arr, 0.95)),
        "frac_positive": float(np.mean(arr > 0)),
    }


def decision_gates(by_profile: pd.DataFrame, raw: pd.DataFrame, integrity: Mapping[str, Any]) -> dict[str, Any]:
    rows = []
    for _, r in by_profile.iterrows():
        name = r["label"]
        g = raw[raw["profile"] == name]
        btc_eth = g[g["symbol"].isin(["BTCUSDT", "ETHUSDT"])]
        oos = g[g["split"] == "oos"]
        # without best coin
        coin_e = g.groupby("symbol")["final_pnl_pct"].mean()
        best = coin_e.idxmax() if len(coin_e) else None
        without_best = g[g["symbol"] != best] if best else g
        val = g[g["split"] == "validation"]
        ref = raw[raw["profile"] == "reference_tp3_sl2"]
        ref_val = ref[ref["split"] == "validation"]
        ref_dd = max_drawdown_pp(ref.sort_values(["entry_time", "symbol"])["final_pnl_pct"]) if len(ref) else None
        ref_streak = max_losing_streak(ref.sort_values(["entry_time", "symbol"])["final_pnl_pct"]) if len(ref) else None
        dd = r.get("max_drawdown_pp")
        streak = r.get("max_losing_streak")
        val_e = _mean(val["final_pnl_pct"]) if len(val) else None
        ref_val_e = _mean(ref_val["final_pnl_pct"]) if len(ref_val) else None
        val_not_much_worse = True
        if val_e is not None and ref_val_e is not None:
            val_not_much_worse = val_e >= ref_val_e - 0.25  # frozen tolerance band, not a new search
        dd_ok = True if dd is None or ref_dd is None else dd >= ref_dd - 10.0
        streak_ok = True if streak is None or ref_streak is None else streak <= ref_streak + 3
        prevented = int(g["full_sl_prevented"].sum()) if "full_sl_prevented" in g else 0
        cut = int(g["winner_cut_off"].sum()) if "winner_cut_off" in g else 0
        delta_vs_ref = float((g.set_index("trade_key")["final_pnl_pct"] - ref.set_index("trade_key")["final_pnl_pct"]).sum()) if name != "reference_tp3_sl2" and len(g) == len(ref) else 0.0
        econ = {
            "pooled_net_gt_0": bool(r.get("net_expectancy") is not None and r["net_expectancy"] > 0),
            "pf_gt_1": bool(r.get("profit_factor") is not None and (r["profit_factor"] == float("inf") or r["profit_factor"] > 1)),
            "btc_eth_positive": bool(_mean(btc_eth["final_pnl_pct"]) is not None and _mean(btc_eth["final_pnl_pct"]) > 0),
            "oos_ge_0": bool(_mean(oos["final_pnl_pct"]) is not None and _mean(oos["final_pnl_pct"]) >= 0),
            "without_best_ge_0": bool(_mean(without_best["final_pnl_pct"]) is not None and _mean(without_best["final_pnl_pct"]) >= 0),
            "validation_not_much_worse": bool(val_not_much_worse),
            "drawdown_not_much_worse": bool(dd_ok),
            "streak_not_much_worse": bool(streak_ok),
            "not_tiny_edge_sample": bool(r.get("n", 0) >= 5),
            "sl_prevent_vs_cutoff_or_net": bool(prevented >= cut or (r.get("net_expectancy") or 0) > 0),
        }
        rows.append(
            {
                "profile": name,
                "economic_pass_count": int(sum(econ.values())),
                "economic_n": len(econ),
                **econ,
                "prevented_sl": prevented,
                "winner_cut_off": cut,
                "sum_delta_vs_reference": delta_vs_ref,
                "integrity_ok": bool(integrity.get("ok")),
            }
        )
    return {"by_profile": rows, "integrity_ok": bool(integrity.get("ok"))}


def run_matrix(
    *,
    input_audit_dir: Path,
    output_dir: Path,
    coins: Sequence[str] = COINS,
    profiles: Sequence[str] | None = None,
    resume: bool = False,
    conservative_lock_mode: str = CONSERVATIVE_LOCK_MODE,
    print_manual_commands_only: bool = False,
) -> dict[str, Any]:
    out = Path(output_dir)
    if out.resolve() in FORBIDDEN_OVERWRITE:
        raise ValueError(f"refusing to overwrite reference dir: {out}")
    assert_safe_output_dir(out)

    manual = {
        "matrix_run": (
            f"PYTHONPATH=. python -m research.regime_scanner.run_short_rr_be_lock_matrix "
            f"--input-audit-dir {input_audit_dir} --output-dir {out} "
            f"--conservative-lock-mode {conservative_lock_mode}"
        ),
        "later_forward_holdout_nohup": (
            "nohup env PYTHONPATH=. python -m research.regime_scanner.run_short_rr_be_lock_matrix "
            f"--input-audit-dir <NEW_TEMPORAL_HOLDOUT_AUDIT_DIR> "
            f"--output-dir research/regime_scanner/results/signal_path_audit_15m_forward_short_rr_be_lock_<DATE> "
            f"--coins BTCUSDT ETHUSDT BNBUSDT "
            f"--profiles <WINNER_PROFILE> "
            f"--conservative-lock-mode {conservative_lock_mode} "
            f"> research/regime_scanner/results/forward_rr_be_lock_<DATE>.log 2>&1 &"
        ),
        "note": "Do not start forward holdout until a winner is chosen from this matrix.",
    }
    if print_manual_commands_only:
        print(json.dumps(manual, indent=2))
        return {"manual_commands": manual}

    out.mkdir(parents=True, exist_ok=True)
    selected = list(profiles) if profiles else [p.name for p in PROFILES]
    for name in selected:
        if name not in PROFILE_BY_NAME:
            raise KeyError(f"unknown profile: {name}")

    frozen = {
        "profiles": [PROFILE_BY_NAME[n].__dict__ for n in selected],
        "cost_pct": COST_PCT,
        "slippage_pct": SLIPPAGE_PCT,
        "horizon_bars": HORIZON_BARS,
        "lock_mode": conservative_lock_mode,
        "be_formula": "short px = entry / (1 + (cost+slip)/100); ceil to tick",
        "progress_formula": "(entry - favorable_price) / (entry - tp_price)",
        "intrabar": "conservative_next_bar_lock; same-bar SL/BE before TP",
        "no_new_thresholds_after_results": True,
    }
    (out / "frozen_matrix.json").write_text(json.dumps(json_safe(frozen), indent=2) + "\n", encoding="utf-8")

    shorts = load_short_trades(input_audit_dir, coins)
    checkpoint_path = out / "checkpoint.json"
    raw_path = out / "raw_trades.csv"
    done_profiles: set[str] = set()
    parts: list[pd.DataFrame] = []
    if resume and raw_path.is_file() and checkpoint_path.is_file():
        prev = pd.read_csv(raw_path)
        parts.append(prev)
        done_profiles = set(json.loads(checkpoint_path.read_text()).get("done_profiles", []))

    # cache OHLC
    ohlc: dict[str, Any] = {}
    for sym in sorted(set(shorts["symbol"])):
        ohlc[sym] = load_symbol_ohlc(str(sym))

    for pname in selected:
        if pname in done_profiles:
            continue
        prof = PROFILE_BY_NAME[pname]
        rows = []
        for _, tr in shorts.iterrows():
            sym = str(tr["symbol"])
            oc = ohlc[sym]
            fill_i = int(tr["fill_bar"])
            entry = float(tr["entry_price"])
            sim = simulate_short_exit(
                profile=prof,
                symbol=sym,
                entry=entry,
                fill_i=fill_i,
                highs=oc["highs"],
                lows=oc["lows"],
                closes=oc["closes"],
                timestamps=oc["timestamps"],
                n_bars=oc["n_bars"],
                lock_mode=conservative_lock_mode,
            )
            rows.append(
                {
                    "profile": pname,
                    "trade_key": tr["trade_key"],
                    "coin": sym,
                    "symbol": sym,
                    "split": tr["split"],
                    "signal_id": tr.get("setup_id"),
                    "entry_time": tr["fill_timestamp"],
                    "entry_price": entry,
                    "fill_bar": fill_i,
                    "original_exit_type": tr.get("exit_reason"),
                    "hypothetical_reference_exit": tr.get("exit_reason"),
                    **sim,
                }
            )
        part = pd.DataFrame(rows)
        parts.append(part)
        done_profiles.add(pname)
        raw_all = pd.concat(parts, ignore_index=True)
        raw_all.to_csv(raw_path, index=False)
        checkpoint_path.write_text(
            json.dumps({"done_profiles": sorted(done_profiles), "n_rows": int(len(raw_all))}, indent=2) + "\n",
            encoding="utf-8",
        )

    raw = pd.concat(parts, ignore_index=True)
    # attach pairwise diagnostics vs no_lock sibling and reference
    ref = raw[raw["profile"] == "reference_tp3_sl2"].set_index("trade_key")
    enriched_parts = []
    for pname, g in raw.groupby("profile"):
        gg = g.copy()
        # find no_lock sibling
        base_name = None
        if "_lock" in pname:
            base_name = pname.split("_lock")[0] + "_no_lock"
        elif pname.endswith("_no_lock"):
            base_name = pname
        if base_name and base_name in set(raw["profile"]):
            base = raw[raw["profile"] == base_name].set_index("trade_key")
            deltas = []
            prevented = []
            cut = []
            for _, r in gg.iterrows():
                b = base.loc[r["trade_key"]]
                deltas.append(float(r["final_pnl_pct"] - b["final_pnl_pct"]))
                prevented.append(
                    bool(
                        b["final_exit_type"] in {"SL", "same_bar_conservative_sl"}
                        and r["final_exit_type"] not in {"SL", "same_bar_conservative_sl"}
                    )
                )
                cut.append(bool(b["final_exit_type"] == "TP" and r["final_exit_type"] == "lock_be"))
            gg["pnl_delta_vs_no_lock"] = deltas
            gg["full_sl_prevented"] = prevented
            gg["winner_cut_off"] = cut
        else:
            gg["pnl_delta_vs_no_lock"] = 0.0
            gg["full_sl_prevented"] = False
            gg["winner_cut_off"] = False
        if len(ref):
            gg["pnl_delta_vs_reference_tp3_sl2"] = [
                float(r["final_pnl_pct"] - ref.loc[r["trade_key"], "final_pnl_pct"]) if r["trade_key"] in ref.index else None
                for _, r in gg.iterrows()
            ]
        else:
            gg["pnl_delta_vs_reference_tp3_sl2"] = None
        enriched_parts.append(gg)
    raw = pd.concat(enriched_parts, ignore_index=True)
    raw.to_csv(raw_path, index=False)

    # aggregates
    by_profile = pd.DataFrame([summarize_slice(g, label=str(p)) for p, g in raw.groupby("profile")])
    by_split_rows = []
    for (p, sp), g in raw.groupby(["profile", "split"]):
        r = summarize_slice(g, label=f"{p}|{sp}")
        r["profile"] = p
        r["split"] = sp
        by_split_rows.append(r)
    by_split = pd.DataFrame(by_split_rows)
    by_coin_rows = []
    for (p, sym), g in raw.groupby(["profile", "symbol"]):
        r = summarize_slice(g, label=f"{p}|{sym}")
        r["profile"] = p
        r["symbol"] = sym
        by_coin_rows.append(r)
    by_coin = pd.DataFrame(by_coin_rows)

    btc_eth_rows = []
    for p, g in raw.groupby("profile"):
        ge = g[g["symbol"].isin(["BTCUSDT", "ETHUSDT"])]
        r = summarize_slice(ge, label=f"{p}|BTC+ETH")
        r["profile"] = p
        btc_eth_rows.append(r)
        # without BNB same
        r2 = summarize_slice(g[g["symbol"] != "BNBUSDT"], label=f"{p}|without_BNB")
        r2["profile"] = p
        btc_eth_rows.append(r2)
        # equal coin
        coin_e = g.groupby("symbol")["final_pnl_pct"].mean()
        btc_eth_rows.append(
            {
                "label": f"{p}|equal_coin",
                "profile": p,
                "n": int(len(g)),
                "equal_mean": float(coin_e.mean()) if len(coin_e) else None,
                "equal_median": float(coin_e.median()) if len(coin_e) else None,
                "pct_coins_positive": float((coin_e > 0).mean()) if len(coin_e) else None,
            }
        )
        # without best
        if len(coin_e):
            best = coin_e.idxmax()
            wb = summarize_slice(g[g["symbol"] != best], label=f"{p}|without_best")
            wb["profile"] = p
            wb["excluded"] = best
            btc_eth_rows.append(wb)
    btc_eth = pd.DataFrame(btc_eth_rows)

    # lock activation analysis
    lock_rows = []
    for p, g in raw.groupby("profile"):
        if not bool(g["lock_enabled"].iloc[0]):
            continue
        lock_rows.append(
            {
                "profile": p,
                "n": int(len(g)),
                "activation_rate": float(g["lock_activated"].mean()),
                "lock_exit_rate": float((g["final_exit_type"] == "lock_be").mean()),
                "activated_then_tp": float(((g["lock_activated"]) & (g["final_exit_type"] == "TP")).mean()),
                "prevented_sl": int(g["full_sl_prevented"].sum()),
                "winner_cut_off": int(g["winner_cut_off"].sum()),
                "hyp_tp_after_lock": int(g["hypothetical_tp_after_lock_exit"].sum()),
                "mean_delta_vs_no_lock": _mean(g["pnl_delta_vs_no_lock"]),
            }
        )
    lock_act = pd.DataFrame(lock_rows)

    # transitions
    trans_rows = []
    for p, g in raw.groupby("profile"):
        if "_lock" not in p:
            continue
        base_name = p.split("_lock")[0] + "_no_lock"
        if base_name not in set(raw["profile"]):
            continue
        base = raw[raw["profile"] == base_name].set_index("trade_key")
        for _, r in g.iterrows():
            b = base.loc[r["trade_key"]]
            trans_rows.append(
                {
                    "profile": p,
                    "trade_key": r["trade_key"],
                    "from_exit": b["final_exit_type"],
                    "to_exit": r["final_exit_type"],
                    "pnl_delta": float(r["final_pnl_pct"] - b["final_pnl_pct"]),
                    "lock_activated": bool(r["lock_activated"]),
                }
            )
    transitions = pd.DataFrame(trans_rows)

    # pairwise tables
    pair_nolock = []
    pair_ref = []
    for p in selected:
        if p == "reference_tp3_sl2":
            continue
        g = raw[raw["profile"] == p]
        if "_lock" in p:
            base_name = p.split("_lock")[0] + "_no_lock"
            if base_name in selected:
                pair_nolock.append(pairwise_vs(raw[raw.profile == base_name], g, base_name=base_name, other_name=p))
        pair_ref.append(
            pairwise_vs(raw[raw.profile == "reference_tp3_sl2"], g, base_name="reference_tp3_sl2", other_name=p)
        )
    # also RR no_lock vs reference
    for p in selected:
        if p.endswith("_no_lock"):
            pair_ref.append(
                pairwise_vs(raw[raw.profile == "reference_tp3_sl2"], raw[raw.profile == p], base_name="reference_tp3_sl2", other_name=p)
            )

    # worst cases: largest negative deltas vs no_lock among lock profiles
    worst = (
        raw[raw["profile"].str.contains("_lock")]
        .sort_values("pnl_delta_vs_no_lock", ascending=True)
        .head(50)
    )

    # bootstrap per profile
    boot = {p: bootstrap_expectancy(g["final_pnl_pct"]) for p, g in raw.groupby("profile")}

    # integrity
    integrity = {
        "ok": True,
        "n_input_shorts": int(len(shorts)),
        "n_profiles": len(selected),
        "expected_rows": int(len(shorts) * len(selected)),
        "actual_rows": int(len(raw)),
        "duplicate_keys_per_profile": False,
        "longs_present": bool((raw.get("side", pd.Series(dtype=str)) == "long").any()) if "side" in raw else False,
        "lock_mode": conservative_lock_mode,
        "issues": [],
    }
    for p, g in raw.groupby("profile"):
        if g["trade_key"].duplicated().any():
            integrity["ok"] = False
            integrity["duplicate_keys_per_profile"] = True
            integrity["issues"].append(f"dup keys in {p}")
        if set(g["trade_key"]) != set(shorts["trade_key"]):
            integrity["ok"] = False
            integrity["issues"].append(f"key set mismatch in {p}")
        if int(len(g)) != int(len(shorts)):
            integrity["ok"] = False
            integrity["issues"].append(f"count mismatch in {p}")
    # no retroactive lock: lock_active_from > lock_trigger_candle when activated
    lock_g = raw[raw["lock_activated"] == True]  # noqa: E712
    if len(lock_g):
        bad = lock_g[lock_g["lock_active_from_bar"] <= lock_g["lock_trigger_candle"]]
        if len(bad):
            integrity["ok"] = False
            integrity["issues"].append(f"retroactive lock rows={len(bad)}")

    decision = decision_gates(by_profile, raw, integrity)

    # write outputs
    by_profile.to_csv(out / "summary_by_profile.csv", index=False)
    by_split.to_csv(out / "summary_by_split.csv", index=False)
    by_coin.to_csv(out / "summary_by_coin.csv", index=False)
    btc_eth.to_csv(out / "summary_btc_eth.csv", index=False)
    lock_act.to_csv(out / "lock_activation_analysis.csv", index=False)
    transitions.to_csv(out / "lock_trade_transitions.csv", index=False)
    pd.DataFrame(pair_nolock).to_csv(out / "pairwise_vs_no_lock.csv", index=False)
    pd.DataFrame(pair_ref).drop_duplicates(subset=["base", "other"]).to_csv(out / "pairwise_vs_reference.csv", index=False)
    worst.to_csv(out / "worst_cases.csv", index=False)
    (out / "bootstrap_results.json").write_text(json.dumps(json_safe(boot), indent=2) + "\n", encoding="utf-8")
    (out / "integrity.json").write_text(json.dumps(json_safe(integrity), indent=2) + "\n", encoding="utf-8")
    (out / "decision_preliminary.json").write_text(json.dumps(json_safe(decision), indent=2) + "\n", encoding="utf-8")
    by_profile.to_csv(out / "aggregate_summary.csv", index=False)

    # ranking
    rank = by_profile.sort_values("net_expectancy", ascending=False, kind="mergesort")
    lines = [
        "# Short RR 1:2 + Break-even Lock Matrix",
        "",
        "Research-only. **No A6/Pine/signal changes. No live recommendation.**",
        "",
        f"- Input: `{input_audit_dir}`",
        f"- Lock mode: `{conservative_lock_mode}`",
        f"- Cost: `{COST_PCT}%` RT · Slippage modeled: `{SLIPPAGE_PCT}%`",
        f"- Short trades: `{len(shorts)}` · Profiles: `{len(selected)}`",
        f"- Integrity ok: `{integrity['ok']}`",
        "",
        "## Ranking by net expectancy",
        "",
        rank[["label", "n", "net_expectancy", "profit_factor", "winrate", "n_lock_be", "full_sl_prevented", "winner_cut_off", "max_drawdown_pp", "max_losing_streak"]].to_string(index=False),
        "",
        "## Manual later forward command",
        "",
        "```",
        manual["later_forward_holdout_nohup"],
        "```",
        "",
    ]
    (out / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    manifest = {
        "audit": "short_rr_be_lock_matrix",
        "input_audit_dir": str(input_audit_dir),
        "output_dir": str(out),
        "coins": list(coins),
        "profiles": selected,
        "n_trades": int(len(shorts)),
        "integrity_ok": integrity["ok"],
        "cost_pct": COST_PCT,
        "lock_mode": conservative_lock_mode,
        "manual_commands": manual,
        "input_sha1_trade_csv": hashlib.sha1((Path(input_audit_dir) / "multicoin_trade_results.csv").read_bytes()).hexdigest(),
    }
    (out / "run_manifest.json").write_text(json.dumps(json_safe(manifest), indent=2) + "\n", encoding="utf-8")
    return manifest


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Short RR1:2 + BE lock matrix")
    p.add_argument("--input-audit-dir", type=Path, default=DEFAULT_INPUT)
    p.add_argument("--output-dir", type=Path, default=DEFAULT_OUT)
    p.add_argument("--coins", nargs="*", default=list(COINS))
    p.add_argument("--profiles", nargs="*", default=None)
    p.add_argument("--resume", action="store_true")
    p.add_argument("--criteria-profile", default="holdout")
    p.add_argument("--conservative-lock-mode", default=CONSERVATIVE_LOCK_MODE)
    p.add_argument("--print-manual-commands-only", action="store_true")
    args = p.parse_args(list(argv) if argv is not None else None)
    meta = run_matrix(
        input_audit_dir=args.input_audit_dir,
        output_dir=args.output_dir,
        coins=args.coins,
        profiles=args.profiles,
        resume=args.resume,
        conservative_lock_mode=args.conservative_lock_mode,
        print_manual_commands_only=args.print_manual_commands_only,
    )
    print(json.dumps(json_safe({"ok": True, "out": str(args.output_dir), "integrity": meta.get("integrity_ok"), "n_trades": meta.get("n_trades")})))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

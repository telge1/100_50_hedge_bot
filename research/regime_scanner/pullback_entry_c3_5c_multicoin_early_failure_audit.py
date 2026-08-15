"""Early-failure / early-exit audit on post-entry path checkpoints (diagnostic only).

Predefined candidates F1–F6 only. No automatic activation. No A6/Pine changes.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any, Callable

import numpy as np
import pandas as pd

from research.regime_scanner.c35c_signal_store.path_schema import DEFAULT_PATH_VERSION
from research.regime_scanner.c35c_signal_store.path_store import C35cPathStore
from research.regime_scanner.candle_sources import load_regime_db_env_file
from research.regime_scanner.mysql_candle_store.config import load_regime_db_config
from research.regime_scanner.point_audit import json_safe
from research.regime_scanner.pullback_entry_c3_5c_fill_excursion_audit import (
    COST_ROUNDTRIP_PCT,
    signed_return_pct,
)
from research.regime_scanner.trend_weakening_multi_bar_audit import assert_safe_output_dir

DEFAULT_OUT = Path("research/regime_scanner/results/multicoin_early_failure_audit_20260722")
DEFAULT_ENV = Path(
    "/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/"
    "research/regime_scanner/.env.regime_db"
)
DEFAULT_OUTCOME_VERSION = "tp3_sl2_h192_cost020_v1"
COST_PCT = COST_ROUNDTRIP_PCT

# Predefined failure candidates (no free search).
CANDIDATE_DEFINITIONS: list[dict[str, Any]] = [
    {"id": "F1_no_mfe", "family": "F1", "desc": "MFE so far <= 0"},
    {"id": "F1_mfe_lt_0_10", "family": "F1", "desc": "MFE so far < 0.10%"},
    {"id": "F1_mfe_lt_0_25", "family": "F1", "desc": "MFE so far < 0.25%"},
    {"id": "F2_breakout_lost", "family": "F2", "desc": "Breakout level lost on close"},
    {
        "id": "F2_breakout_lost_not_reclaimed",
        "family": "F2",
        "desc": "Breakout lost and not reclaimed",
    },
    {"id": "F3_counter_micro_bos", "family": "F3", "desc": "Micro counter BOS ever"},
    {"id": "F3_counter_micro_choch", "family": "F3", "desc": "Micro counter CHOCH ever"},
    {"id": "F3_counter_micro_any", "family": "F3", "desc": "Micro BOS or CHOCH against"},
    {"id": "F4_ema_alignment_lost", "family": "F4", "desc": "EMA9/20 alignment lost"},
    {"id": "F5_mae_le_0_25", "family": "F5", "desc": "MAE <= -0.25%"},
    {"id": "F5_mae_le_0_50", "family": "F5", "desc": "MAE <= -0.50%"},
    {"id": "F5_mae_le_0_75", "family": "F5", "desc": "MAE <= -0.75%"},
    {"id": "F5_mae_le_1_00", "family": "F5", "desc": "MAE <= -1.00%"},
    {"id": "F6_no_mfe_and_breakout_lost", "family": "F6", "desc": "No MFE AND breakout lost"},
    {
        "id": "F6_breakout_lost_and_counter_bos",
        "family": "F6",
        "desc": "Breakout lost AND counter micro BOS",
    },
    {"id": "F6_no_mfe_and_mae_le_0_50", "family": "F6", "desc": "No MFE AND MAE <= -0.50%"},
    {
        "id": "F6_ema_lost_and_breakout_lost",
        "family": "F6",
        "desc": "EMA alignment lost AND breakout lost",
    },
]


def _parse_feature_json(v: Any) -> dict[str, Any]:
    if v is None:
        return {}
    if isinstance(v, dict):
        return v
    if isinstance(v, (bytes, bytearray)):
        v = v.decode()
    if isinstance(v, str):
        try:
            return json.loads(v)
        except json.JSONDecodeError:
            return {}
    return {}


def rule_fires(candidate_id: str, row: pd.Series) -> bool:
    mfe = row.get("mfe_so_far_pct")
    mae = row.get("mae_so_far_pct")
    bl = bool(int(row.get("breakout_level_lost") or 0))
    br = bool(int(row.get("breakout_level_reclaimed") or 0))
    bos = bool(int(row.get("micro_counter_bos") or 0))
    choch = bool(int(row.get("micro_counter_choch") or 0))
    ema_lost = bool(int(row.get("ema9_20_lost") or 0))
    no_mfe = bool(mfe is not None and pd.notna(mfe) and float(mfe) <= 0)
    if candidate_id == "F1_no_mfe":
        return no_mfe
    if candidate_id == "F1_mfe_lt_0_10":
        return bool(mfe is not None and pd.notna(mfe) and float(mfe) < 0.10)
    if candidate_id == "F1_mfe_lt_0_25":
        return bool(mfe is not None and pd.notna(mfe) and float(mfe) < 0.25)
    if candidate_id == "F2_breakout_lost":
        return bl
    if candidate_id == "F2_breakout_lost_not_reclaimed":
        return bl and not br
    if candidate_id == "F3_counter_micro_bos":
        return bos
    if candidate_id == "F3_counter_micro_choch":
        return choch
    if candidate_id == "F3_counter_micro_any":
        return bos or choch
    if candidate_id == "F4_ema_alignment_lost":
        return ema_lost
    if candidate_id == "F5_mae_le_0_25":
        return bool(mae is not None and pd.notna(mae) and float(mae) <= -0.25)
    if candidate_id == "F5_mae_le_0_50":
        return bool(mae is not None and pd.notna(mae) and float(mae) <= -0.50)
    if candidate_id == "F5_mae_le_0_75":
        return bool(mae is not None and pd.notna(mae) and float(mae) <= -0.75)
    if candidate_id == "F5_mae_le_1_00":
        return bool(mae is not None and pd.notna(mae) and float(mae) <= -1.00)
    if candidate_id == "F6_no_mfe_and_breakout_lost":
        return no_mfe and bl
    if candidate_id == "F6_breakout_lost_and_counter_bos":
        return bl and bos
    if candidate_id == "F6_no_mfe_and_mae_le_0_50":
        return no_mfe and bool(mae is not None and pd.notna(mae) and float(mae) <= -0.50)
    if candidate_id == "F6_ema_lost_and_breakout_lost":
        return ema_lost and bl
    raise KeyError(candidate_id)


def _side_sign(direction: str) -> int:
    return 1 if str(direction).lower() == "long" else -1


def _profit_factor(pnls: np.ndarray) -> float | None:
    wins = pnls[pnls > 0].sum()
    losses = -pnls[pnls < 0].sum()
    if losses < 1e-15:
        return None if wins <= 0 else float("inf")
    return float(wins / losses)


def _max_dd(pnls: np.ndarray) -> float:
    if len(pnls) == 0:
        return 0.0
    eq = np.cumsum(pnls)
    peak = np.maximum.accumulate(eq)
    dd = eq - peak
    return float(dd.min()) if len(dd) else 0.0


def _max_losing_streak(pnls: np.ndarray) -> int:
    best = 0
    cur = 0
    for x in pnls:
        if x < 0:
            cur += 1
            best = max(best, cur)
        else:
            cur = 0
    return int(best)


def baseline_metrics(pnls: np.ndarray) -> dict[str, Any]:
    pnls = np.asarray(pnls, dtype=float)
    pnls = pnls[np.isfinite(pnls)]
    return {
        "n": int(len(pnls)),
        "expectation": float(np.mean(pnls)) if len(pnls) else None,
        "sum_pnl": float(np.sum(pnls)) if len(pnls) else 0.0,
        "pf": _profit_factor(pnls),
        "max_dd": _max_dd(pnls),
        "max_losing_streak": _max_losing_streak(pnls),
        "win_rate": float(np.mean(pnls > 0)) if len(pnls) else None,
    }


def simulate_early_exit_row(
    *,
    side: int,
    entry: float,
    baseline_net: float,
    baseline_reason: str,
    bars_held: int | None,
    checkpoint_bar: int,
    feature_json: dict[str, Any],
    cost_pct: float = COST_PCT,
) -> dict[str, Any]:
    """Exit at next open after checkpoint close; no same-candle backdate."""
    bsf = int(checkpoint_bar) - 1
    still_open = feature_json.get("still_open_after_checkpoint")
    if still_open is None:
        still_open = bars_held is None or int(bars_held) > bsf
    next_open = feature_json.get("next_open_price")
    if not still_open:
        return {
            "early_exit_applied": False,
            "skip_reason": "already_exited",
            "early_exit_net_pnl_pct": None,
            "delta_pnl_pct": 0.0,
        }
    if next_open is None or (isinstance(next_open, float) and not math.isfinite(next_open)):
        return {
            "early_exit_applied": False,
            "skip_reason": "next_open_unavailable",
            "early_exit_net_pnl_pct": None,
            "delta_pnl_pct": 0.0,
        }
    gross = float(signed_return_pct(side, entry, float(next_open)))
    net = gross - float(cost_pct)
    return {
        "early_exit_applied": True,
        "skip_reason": None,
        "early_exit_price": float(next_open),
        "early_exit_gross_pnl_pct": gross,
        "early_exit_net_pnl_pct": net,
        "baseline_net_pnl_pct": float(baseline_net),
        "delta_pnl_pct": net - float(baseline_net),
        "avoided_loss": max(0.0, float(baseline_net) - net) if baseline_net < net else 0.0,
        "truncated_gain": max(0.0, float(baseline_net) - net) if baseline_net > net else 0.0,
        "baseline_exit_reason": baseline_reason,
    }


def load_audit_panel(
    store: C35cPathStore,
    *,
    parent_run_label: str,
    path_version: str,
    outcome_version: str,
) -> pd.DataFrame:
    children = store.find_child_runs(parent_run_label)
    if not children:
        raise RuntimeError(f"no child runs for {parent_run_label}")
    run_ids = [str(c["run_id"]) for c in children]
    sym_by_run = {str(c["run_id"]): str(c.get("symbol") or "").upper() for c in children}

    cps = store.load_checkpoints(path_version=path_version, run_ids=run_ids)
    labels = store.load_labels(path_version=path_version, run_ids=run_ids)
    label_by_sid = {int(r["signal_id"]): r for r in labels}

    sig_rows: list[dict[str, Any]] = []
    out_rows: list[dict[str, Any]] = []
    for run in children:
        rid = str(run["run_id"])
        sigs, outcomes, _trig, fill = store.load_signals_bundle(rid, outcome_version=outcome_version)
        for s in sigs:
            meta = s.get("metadata_json") or {}
            if isinstance(meta, str):
                try:
                    meta = json.loads(meta)
                except json.JSONDecodeError:
                    meta = {}
            sig_rows.append(
                {
                    "signal_id": int(s["id"]),
                    "run_id": rid,
                    "symbol": sym_by_run.get(rid) or str(meta.get("symbol") or "").upper(),
                    "direction": s.get("direction"),
                    "entry_price": float(s["entry_price"]),
                    "entry_time": s.get("entry_time"),
                    "split": (meta.get("split") if isinstance(meta, dict) else None)
                    or (fill.get(int(s["id"]), {}) or {}).get("split"),
                    "signal_key": s.get("signal_key"),
                }
            )
            oc = outcomes.get(int(s["id"]))
            if oc:
                out_rows.append({"signal_id": int(s["id"]), **{k: oc[k] for k in oc if k != "id"}})

    sig_df = pd.DataFrame(sig_rows)
    out_df = pd.DataFrame(out_rows)
    cp_df = pd.DataFrame(cps)
    if cp_df.empty:
        raise RuntimeError("no path checkpoints found — run path store persist first")
    cp_df["feature_json_parsed"] = cp_df["feature_json"].map(_parse_feature_json)
    panel = cp_df.merge(sig_df, on=["signal_id", "run_id"], how="left", suffixes=("", "_sig"))
    if not out_df.empty:
        keep = [
            "signal_id",
            "exit_reason",
            "net_pnl_pct",
            "gross_pnl_pct",
            "bars_held",
            "bars_to_tp",
            "bars_to_sl",
            "is_winner",
            "mfe_pct",
            "mae_pct",
            "same_bar_ambiguous",
            "time_exit",
            "data_end",
        ]
        cols = [c for c in keep if c in out_df.columns]
        panel = panel.merge(out_df[cols], on="signal_id", how="left")
    panel["path_type"] = panel["signal_id"].map(
        lambda sid: (label_by_sid.get(int(sid)) or {}).get("path_type")
    )
    # fill split from fill features if missing
    if "split" not in panel.columns or panel["split"].isna().all():
        panel["split"] = None
    return panel


def _eval_slice(
    panel_ok: pd.DataFrame,
    candidate_id: str,
    checkpoint_bar: int,
    *,
    slice_name: str,
) -> dict[str, Any]:
    sub = panel_ok[panel_ok["checkpoint_bar"].astype(int) == int(checkpoint_bar)].copy()
    if sub.empty:
        return {
            "candidate_id": candidate_id,
            "checkpoint_bar": checkpoint_bar,
            "slice": slice_name,
            "n_signals": 0,
            "n_triggered": 0,
        }
    # one row per signal at this CP
    sub = sub.drop_duplicates(subset=["signal_id"], keep="first")
    fires = sub.apply(lambda r: rule_fires(candidate_id, r), axis=1)
    trig = sub[fires].copy()
    base_pnls = pd.to_numeric(sub["net_pnl_pct"], errors="coerce").to_numpy(dtype=float)
    base = baseline_metrics(base_pnls)

    new_pnls = []
    trade_level = []
    for _, r in sub.iterrows():
        fired = bool(rule_fires(candidate_id, r))
        side = _side_sign(r.get("direction"))
        fj = r.get("feature_json_parsed") or {}
        if not isinstance(fj, dict):
            fj = _parse_feature_json(fj)
        sim = (
            simulate_early_exit_row(
                side=side,
                entry=float(r["entry_price"]),
                baseline_net=float(r["net_pnl_pct"]),
                baseline_reason=str(r.get("exit_reason")),
                bars_held=None if pd.isna(r.get("bars_held")) else int(r.get("bars_held")),
                checkpoint_bar=int(checkpoint_bar),
                feature_json=fj,
            )
            if fired
            else {
                "early_exit_applied": False,
                "skip_reason": "rule_not_fired",
                "early_exit_net_pnl_pct": None,
                "delta_pnl_pct": 0.0,
            }
        )
        final = (
            float(sim["early_exit_net_pnl_pct"])
            if sim.get("early_exit_applied")
            else float(r["net_pnl_pct"])
        )
        new_pnls.append(final)
        if fired:
            trade_level.append(
                {
                    "candidate_id": candidate_id,
                    "checkpoint_bar": checkpoint_bar,
                    "slice": slice_name,
                    "signal_id": int(r["signal_id"]),
                    "symbol": r.get("symbol"),
                    "direction": r.get("direction"),
                    "path_type": r.get("path_type"),
                    "split": r.get("split"),
                    "baseline_net_pnl_pct": float(r["net_pnl_pct"]),
                    "baseline_exit_reason": r.get("exit_reason"),
                    "is_baseline_winner": bool(float(r["net_pnl_pct"]) > 0),
                    **sim,
                    "final_net_pnl_pct": final,
                }
            )

    new = baseline_metrics(np.asarray(new_pnls, dtype=float))
    n_trig = int(fires.sum())
    winners_cut = int(
        ((pd.to_numeric(trig["net_pnl_pct"], errors="coerce") > 0) if n_trig else pd.Series(dtype=bool)).sum()
    ) if n_trig else 0
    losers_cut = int(
        ((pd.to_numeric(trig["net_pnl_pct"], errors="coerce") <= 0) if n_trig else pd.Series(dtype=bool)).sum()
    ) if n_trig else 0
    applied = [t for t in trade_level if t.get("early_exit_applied")]
    deltas = [float(t["delta_pnl_pct"]) for t in applied]
    return {
        "candidate_id": candidate_id,
        "checkpoint_bar": int(checkpoint_bar),
        "slice": slice_name,
        "n_signals": int(len(sub)),
        "n_triggered": n_trig,
        "trigger_rate": float(n_trig / len(sub)) if len(sub) else None,
        "winners_cut": winners_cut,
        "losers_cut": losers_cut,
        "precision_on_losers": float(losers_cut / n_trig) if n_trig else None,
        "recall_losers": (
            float(losers_cut / max(1, int((pd.to_numeric(sub["net_pnl_pct"], errors="coerce") <= 0).sum())))
            if len(sub)
            else None
        ),
        "winner_damage_rate": float(winners_cut / n_trig) if n_trig else None,
        "avg_baseline_pnl_triggered": (
            float(pd.to_numeric(trig["net_pnl_pct"], errors="coerce").mean()) if n_trig else None
        ),
        "avg_early_exit_pnl": (
            float(np.mean([t["early_exit_net_pnl_pct"] for t in applied])) if applied else None
        ),
        "avg_delta": float(np.mean(deltas)) if deltas else None,
        "sum_delta": float(np.sum(deltas)) if deltas else 0.0,
        "expectation_before": base["expectation"],
        "expectation_after": new["expectation"],
        "sum_pnl_before": base["sum_pnl"],
        "sum_pnl_after": new["sum_pnl"],
        "pf_before": base["pf"],
        "pf_after": new["pf"],
        "max_dd_before": base["max_dd"],
        "max_dd_after": new["max_dd"],
        "losing_streak_before": base["max_losing_streak"],
        "losing_streak_after": new["max_losing_streak"],
        "trade_retention": float((len(sub) - len(applied)) / len(sub)) if len(sub) else None,
        "n_early_exit_applied": len(applied),
        "extra_cost_note": f"early exit uses same RT cost {COST_PCT}% (no double-count beyond one exit)",
        "_trades": trade_level,
    }


def evaluate_success_gates(row: dict[str, Any], *, coin_delta_share: float | None) -> dict[str, Any]:
    """Phase 11 gates — recommend falsification only if all pass."""
    checks = {
        "expectation_improved": (
            row.get("expectation_after") is not None
            and row.get("expectation_before") is not None
            and float(row["expectation_after"]) > float(row["expectation_before"])
        ),
        "pf_improved": (
            row.get("pf_after") is not None
            and row.get("pf_before") is not None
            and float(row["pf_after"]) > float(row["pf_before"])
        ),
        "dd_not_much_worse": (
            row.get("max_dd_after") is not None
            and row.get("max_dd_before") is not None
            and float(row["max_dd_after"]) >= float(row["max_dd_before"]) - 0.5
        ),
        "more_losers_than_winners_cut": int(row.get("losers_cut") or 0) > int(row.get("winners_cut") or 0),
        "winner_damage_ok": (row.get("winner_damage_rate") or 1) <= 0.45,
        "enough_triggers": int(row.get("n_triggered") or 0) >= 30,
        "coin_majority": coin_delta_share is not None and coin_delta_share >= 0.60,
    }
    checks["all_core"] = all(
        [
            checks["expectation_improved"],
            checks["pf_improved"],
            checks["dd_not_much_worse"],
            checks["more_losers_than_winners_cut"],
            checks["winner_damage_ok"],
            checks["enough_triggers"],
        ]
    )
    return checks


TOP3 = {"BTCUSDT", "ETHUSDT", "SOLUSDT"}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--parent-run-label", required=True)
    p.add_argument("--path-version", default=DEFAULT_PATH_VERSION)
    p.add_argument("--outcome-version", default=DEFAULT_OUTCOME_VERSION)
    p.add_argument("--regime-db-env", type=Path, default=DEFAULT_ENV)
    p.add_argument("--output-dir", type=Path, default=DEFAULT_OUT)
    p.add_argument("--checkpoints", nargs="+", type=int, default=[1, 2, 3, 4])
    args = p.parse_args(argv)

    assert_safe_output_dir(args.output_dir)
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    load_regime_db_env_file(Path(args.regime_db_env))
    cfg = load_regime_db_config()
    store = C35cPathStore(cfg)
    store.init_schema()

    panel = load_audit_panel(
        store,
        parent_run_label=args.parent_run_label,
        path_version=args.path_version,
        outcome_version=args.outcome_version,
    )
    store.close()

    ok = panel[panel["availability"] == "ok"].copy()
    # feature summary
    feat_rows = []
    for cp in args.checkpoints:
        sub = ok[ok.checkpoint_bar.astype(int) == int(cp)]
        for path_t, g in sub.groupby(sub["path_type"].fillna("unknown")):
            feat_rows.append(
                {
                    "checkpoint_bar": cp,
                    "path_type": path_t,
                    "n": len(g),
                    "mean_mfe": float(pd.to_numeric(g.mfe_so_far_pct, errors="coerce").mean()),
                    "mean_mae": float(pd.to_numeric(g.mae_so_far_pct, errors="coerce").mean()),
                    "pct_breakout_lost": float(pd.to_numeric(g.breakout_level_lost, errors="coerce").mean()),
                    "pct_micro_bos": float(pd.to_numeric(g.micro_counter_bos, errors="coerce").mean()),
                    "pct_ema_lost": float(pd.to_numeric(g.ema9_20_lost, errors="coerce").mean()),
                    "pct_no_mfe": float(pd.to_numeric(g.no_positive_mfe, errors="coerce").mean()),
                    "mean_dir_close": float(
                        pd.to_numeric(g.directional_close_return_pct, errors="coerce").mean()
                    ),
                }
            )
    pd.DataFrame(feat_rows).to_csv(out_dir / "checkpoint_feature_summary.csv", index=False)

    def _pair(a: str, b: str, name: str) -> pd.DataFrame:
        rows = []
        for cp in args.checkpoints:
            for ptype, other in ((a, b),):
                ga = ok[(ok.checkpoint_bar == cp) & (ok.path_type == ptype)]
                gb = ok[(ok.checkpoint_bar == cp) & (ok.path_type == other)]
                rows.append(
                    {
                        "checkpoint_bar": cp,
                        "pair": name,
                        "a": ptype,
                        "b": other,
                        "n_a": len(ga),
                        "n_b": len(gb),
                        "mfe_a": float(pd.to_numeric(ga.mfe_so_far_pct, errors="coerce").mean()) if len(ga) else None,
                        "mfe_b": float(pd.to_numeric(gb.mfe_so_far_pct, errors="coerce").mean()) if len(gb) else None,
                        "mae_a": float(pd.to_numeric(ga.mae_so_far_pct, errors="coerce").mean()) if len(ga) else None,
                        "mae_b": float(pd.to_numeric(gb.mae_so_far_pct, errors="coerce").mean()) if len(gb) else None,
                        "breakout_lost_a": float(pd.to_numeric(ga.breakout_level_lost, errors="coerce").mean())
                        if len(ga)
                        else None,
                        "breakout_lost_b": float(pd.to_numeric(gb.breakout_level_lost, errors="coerce").mean())
                        if len(gb)
                        else None,
                        "micro_bos_a": float(pd.to_numeric(ga.micro_counter_bos, errors="coerce").mean())
                        if len(ga)
                        else None,
                        "micro_bos_b": float(pd.to_numeric(gb.micro_counter_bos, errors="coerce").mean())
                        if len(gb)
                        else None,
                    }
                )
        return pd.DataFrame(rows)

    _pair("direct_winner", "immediate_loser", "direct_winner_vs_immediate_loser").to_csv(
        out_dir / "direct_winner_vs_immediate_loser.csv", index=False
    )
    _pair("reclaim_winner", "delayed_loser", "reclaim_winner_vs_delayed_loser").to_csv(
        out_dir / "reclaim_winner_vs_delayed_loser.csv", index=False
    )
    pd.DataFrame(CANDIDATE_DEFINITIONS).to_csv(out_dir / "failure_candidate_definitions.csv", index=False)

    slice_fns: list[tuple[str, Callable[[pd.DataFrame], pd.DataFrame]]] = [
        ("global", lambda d: d),
        ("long", lambda d: d[d.direction.astype(str).str.lower() == "long"]),
        ("short", lambda d: d[d.direction.astype(str).str.lower() == "short"]),
        ("dev", lambda d: d[d.split.astype(str) == "dev"]),
        ("validation", lambda d: d[d.split.astype(str) == "validation"]),
        ("oos", lambda d: d[d.split.astype(str) == "oos"]),
        ("without_apt", lambda d: d[d.symbol.astype(str) != "APTUSDT"]),
        ("without_top3", lambda d: d[~d.symbol.astype(str).isin(TOP3)]),
        ("path_direct_winner", lambda d: d[d.path_type == "direct_winner"]),
        ("path_immediate_loser", lambda d: d[d.path_type == "immediate_loser"]),
        ("path_reclaim_winner", lambda d: d[d.path_type == "reclaim_winner"]),
        ("path_delayed_loser", lambda d: d[d.path_type == "delayed_loser"]),
    ]

    # common window: intersection of fill times across coins (day-level overlap)
    if "entry_time" in ok.columns:
        et = pd.to_datetime(ok["entry_time"], utc=True)
        ok = ok.copy()
        ok["entry_day"] = et.dt.floor("D")
        day_counts = ok.groupby("entry_day")["symbol"].nunique()
        n_sym = ok["symbol"].nunique()
        common_days = set(day_counts[day_counts >= max(2, n_sym // 2)].index)
        slice_fns.append(("common_window", lambda d, days=common_days: d[d["entry_day"].isin(days)]))

    all_metrics: list[dict[str, Any]] = []
    all_trades: list[dict[str, Any]] = []
    for cand in CANDIDATE_DEFINITIONS:
        cid = cand["id"]
        for cp in args.checkpoints:
            for slice_name, fn in slice_fns:
                sub = fn(ok)
                m = _eval_slice(sub, cid, cp, slice_name=slice_name)
                trades = m.pop("_trades", [])
                all_trades.extend(trades)
                all_metrics.append(m)
            # per coin
            for sym, g in ok.groupby(ok.symbol.astype(str)):
                m = _eval_slice(g, cid, cp, slice_name=f"coin:{sym}")
                m.pop("_trades", None)
                all_metrics.append(m)
            # equal-coin: mean of per-coin expectations delta
            coin_rows = [
                r
                for r in all_metrics
                if r["candidate_id"] == cid
                and r["checkpoint_bar"] == cp
                and str(r["slice"]).startswith("coin:")
            ]
            if coin_rows:
                deltas = []
                for r in coin_rows:
                    if r.get("expectation_before") is not None and r.get("expectation_after") is not None:
                        deltas.append(float(r["expectation_after"]) - float(r["expectation_before"]))
                pos_share = float(np.mean([d > 0 for d in deltas])) if deltas else None
                eq = {
                    "candidate_id": cid,
                    "checkpoint_bar": cp,
                    "slice": "equal_coin",
                    "n_coins": len(coin_rows),
                    "mean_expectation_delta": float(np.mean(deltas)) if deltas else None,
                    "pct_coins_positive_delta": pos_share,
                    "n_triggered": int(np.sum([r.get("n_triggered") or 0 for r in coin_rows])),
                }
                # also full equal-coin sim via mean of coin metrics already stored
                all_metrics.append(eq)

    metrics_df = pd.DataFrame(all_metrics)
    metrics_df.to_csv(out_dir / "failure_candidate_all_slices.csv", index=False)

    def _write_slice(name: str, path: str) -> None:
        sub = metrics_df[metrics_df["slice"] == name]
        sub.to_csv(out_dir / path, index=False)

    _write_slice("global", "failure_candidate_global.csv")
    metrics_df[metrics_df["slice"].isin(["long", "short"])].to_csv(
        out_dir / "failure_candidate_by_side.csv", index=False
    )
    metrics_df[metrics_df["slice"].astype(str).str.startswith("coin:")].to_csv(
        out_dir / "failure_candidate_by_coin.csv", index=False
    )
    metrics_df[metrics_df["slice"].isin(["dev", "validation", "oos"])].to_csv(
        out_dir / "failure_candidate_by_split.csv", index=False
    )
    _write_slice("common_window", "failure_candidate_common_window.csv")
    _write_slice("without_apt", "failure_candidate_without_apt.csv")
    _write_slice("without_top3", "failure_candidate_without_top3.csv")
    _write_slice("equal_coin", "failure_candidate_equal_coin.csv")

    by_cp = (
        metrics_df[metrics_df["slice"] == "global"]
        .groupby(["candidate_id", "checkpoint_bar"], as_index=False)
        .first()
    )
    by_cp.to_csv(out_dir / "failure_candidate_by_checkpoint.csv", index=False)

    glob = metrics_df[metrics_df["slice"] == "global"].copy()
    damage = glob[
        [
            "candidate_id",
            "checkpoint_bar",
            "n_triggered",
            "winners_cut",
            "losers_cut",
            "winner_damage_rate",
            "precision_on_losers",
            "recall_losers",
        ]
    ]
    damage.to_csv(out_dir / "failure_candidate_winner_damage.csv", index=False)
    glob[
        [
            "candidate_id",
            "checkpoint_bar",
            "avg_delta",
            "sum_delta",
            "expectation_before",
            "expectation_after",
            "pf_before",
            "pf_after",
            "max_dd_before",
            "max_dd_after",
        ]
    ].to_csv(out_dir / "failure_candidate_pnl_delta.csv", index=False)

    trades_df = pd.DataFrame(all_trades)
    trades_df.to_csv(out_dir / "early_exit_trade_level.csv", index=False)

    # recommendations
    recs = []
    for side_slice in ("global", "long", "short"):
        side_rows = metrics_df[metrics_df["slice"] == side_slice]
        best = None
        for _, r in side_rows.iterrows():
            eq = metrics_df[
                (metrics_df.candidate_id == r.candidate_id)
                & (metrics_df.checkpoint_bar == r.checkpoint_bar)
                & (metrics_df.slice == "equal_coin")
            ]
            coin_share = None if eq.empty else eq.iloc[0].get("pct_coins_positive_delta")
            wo_apt = metrics_df[
                (metrics_df.candidate_id == r.candidate_id)
                & (metrics_df.checkpoint_bar == r.checkpoint_bar)
                & (metrics_df.slice == "without_apt")
            ]
            wo_top = metrics_df[
                (metrics_df.candidate_id == r.candidate_id)
                & (metrics_df.checkpoint_bar == r.checkpoint_bar)
                & (metrics_df.slice == "without_top3")
            ]
            oos = metrics_df[
                (metrics_df.candidate_id == r.candidate_id)
                & (metrics_df.checkpoint_bar == r.checkpoint_bar)
                & (metrics_df.slice == "oos")
            ]
            gates = evaluate_success_gates(r.to_dict(), coin_delta_share=None if coin_share is None else float(coin_share))
            gates["without_apt_improved"] = (
                not wo_apt.empty
                and wo_apt.iloc[0].get("expectation_after") is not None
                and wo_apt.iloc[0].get("expectation_before") is not None
                and float(wo_apt.iloc[0]["expectation_after"])
                >= float(wo_apt.iloc[0]["expectation_before"])
            )
            gates["without_top3_improved"] = (
                not wo_top.empty
                and wo_top.iloc[0].get("expectation_after") is not None
                and wo_top.iloc[0].get("expectation_before") is not None
                and float(wo_top.iloc[0]["expectation_after"])
                >= float(wo_top.iloc[0]["expectation_before"])
            )
            gates["oos_stable_or_better"] = (
                oos.empty
                or (
                    oos.iloc[0].get("expectation_after") is not None
                    and oos.iloc[0].get("expectation_before") is not None
                    and float(oos.iloc[0]["expectation_after"])
                    >= float(oos.iloc[0]["expectation_before"]) - 0.02
                )
            )
            gates["equal_coin_improved"] = bool(coin_share is not None and float(coin_share) >= 0.60 and (
                not eq.empty
                and eq.iloc[0].get("mean_expectation_delta") is not None
                and float(eq.iloc[0]["mean_expectation_delta"]) > 0
            ))
            gates["pass"] = bool(
                gates["all_core"]
                and gates.get("coin_majority")
                and gates["without_apt_improved"]
                and gates["without_top3_improved"]
                and gates["oos_stable_or_better"]
                and gates["equal_coin_improved"]
            )
            score = (float(r["expectation_after"] or -999) - float(r["expectation_before"] or 0)) if gates[
                "all_core"
            ] else -999
            rec = {
                "slice": side_slice,
                "candidate_id": r.candidate_id,
                "checkpoint_bar": int(r.checkpoint_bar),
                "gates": gates,
                "pass": gates["pass"],
                "score": score,
                "expectation_before": r.get("expectation_before"),
                "expectation_after": r.get("expectation_after"),
                "winner_damage_rate": r.get("winner_damage_rate"),
                "recall_losers": r.get("recall_losers"),
                "n_triggered": r.get("n_triggered"),
            }
            if best is None or (rec["pass"] and (not best["pass"] or rec["score"] > best["score"])):
                best = rec
            elif best is not None and not best["pass"] and not rec["pass"] and score > best["score"]:
                best = rec
        if best:
            recs.append(best)

    # Side-level recommendations only (Phase 11: max one per side).
    side_pass = {r["slice"]: r for r in recs if r.get("pass") and r["slice"] in ("long", "short")}
    global_pass = next((r for r in recs if r.get("pass") and r["slice"] == "global"), None)
    track_rejected = not bool(side_pass)
    recommend = []
    for sl in ("long", "short"):
        if sl in side_pass:
            recommend.append(
                {
                    "side": sl,
                    "candidate_id": side_pass[sl]["candidate_id"],
                    "checkpoint_bar": side_pass[sl]["checkpoint_bar"],
                    "action": "further_falsification_only",
                    "auto_activate": False,
                }
            )

    report = []
    report.append("# Multicoin Early-Failure Audit 2026-07-22\n")
    report.append("Diagnostic only. No automatic activation. No A6/Pine change.\n")
    report.append("## Checkpoint semantics\n")
    report.append("- Fill at open of bar_0; CP N after close of bars_since_fill=N-1\n")
    report.append("- Early exit at next 15m open; no same-candle backdate\n")
    report.append(f"- Cost model: {COST_PCT}% roundtrip (same as baseline)\n")
    report.append("\n## Path types (reused quantile labels)\n")
    if "path_type" in ok.columns:
        vc = ok.drop_duplicates("signal_id")["path_type"].value_counts()
        for k, v in vc.items():
            report.append(f"- {k}: {int(v)}\n")
    report.append("\n## Recommendations\n")
    if track_rejected:
        report.append(
            "**Track rejected** — no Long/Short candidate passes Phase-11 gates "
            "(max one per side; global-only pass is not sufficient for recommendation).\n"
        )
        if global_pass:
            report.append(
                f"- Global near-miss (not recommended): {global_pass['candidate_id']} "
                f"@ CP{global_pass['checkpoint_bar']} "
                f"E {global_pass['expectation_before']}→{global_pass['expectation_after']} "
                f"n_trig={global_pass['n_triggered']} damage={global_pass['winner_damage_rate']}\n"
            )
    else:
        for r in recommend:
            report.append(
                f"- {r['side']}: {r['candidate_id']} @ CP{r['checkpoint_bar']} "
                f"(falsification only, not activated)\n"
            )
    report.append("\n## Best by slice (may fail gates)\n")
    for r in recs:
        report.append(
            f"- {r['slice']}: {r['candidate_id']} CP{r['checkpoint_bar']} "
            f"pass={r['pass']} E {r['expectation_before']}→{r['expectation_after']} "
            f"damage={r['winner_damage_rate']} recall={r['recall_losers']}\n"
        )
    (out_dir / "early_failure_report.md").write_text("".join(report), encoding="utf-8")

    meta = {
        "parent_run_label": args.parent_run_label,
        "path_version": args.path_version,
        "outcome_version": args.outcome_version,
        "n_checkpoint_rows_ok": int(len(ok)),
        "n_signals": int(ok.signal_id.nunique()) if len(ok) else 0,
        "candidates": CANDIDATE_DEFINITIONS,
        "recommendations": recommend,
        "track_rejected": track_rejected,
        "auto_activate": False,
        "a6_changed": False,
        "pine_changed": False,
        "commit": False,
        "push": False,
        "best_by_slice": recs,
    }
    (out_dir / "metadata.json").write_text(json.dumps(json_safe(meta), indent=2), encoding="utf-8")
    print(json.dumps(json_safe(meta), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

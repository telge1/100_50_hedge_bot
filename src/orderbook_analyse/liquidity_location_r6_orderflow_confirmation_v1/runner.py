"""Phase-3 R6 orderflow confirmation runner."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from orderbook_analyse.liquidity_location_pool_edge_validation_v2.stats import block_bootstrap_rate
from orderbook_analyse.orderbook_v2.ch_client import get_clickhouse_client

from . import (
    ANALYSIS_ID,
    ANALYSIS_VERSION,
    R6_CONTRACT,
    T3_WINDOWS_1M,
    T3_WINDOWS_SEC,
    VERDICT_COMPLETE,
    VERDICT_COVERAGE_BLOCKED,
    VERDICT_NO_STABLE,
)
from .coverage import (
    coverage_for_episode,
    fetch_candles_1m,
    fetch_liquidations,
    fetch_ob_agg_1s,
    fetch_oi_5s,
    fetch_trades_1s,
    probe_raw_ob200_available,
)
from .features import (
    build_checkpoint_row,
    edge_reclaim_features,
    extract_ob_features,
    extract_oi_liq_features,
    extract_trade_features,
)
from .r6_contract import DEFAULT_V1, DEFAULT_V2, build_r6_episodes
from .rules import evaluate_rules

DEFAULT_OUT = Path(
    "/home/telgenbuescher/projects/orderbook_analyse/results/liquidity_location_r6_orderflow_confirmation_v1"
)

# Primary decision window for rule features
PRIMARY_T3_SEC = 30


def _write_csv(path: Path, df: pd.DataFrame) -> None:
    if path.exists():
        raise FileExistsError(f"refusing to overwrite {path}")
    df.to_csv(path, index=False)


def assign_coverage_splits(df: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Chronological 60/20/20 on analyzable episodes; keep episode intact."""
    out = df.sort_values("known_at_ts").reset_index(drop=True).copy()
    q60 = out["known_at_ts"].quantile(0.60)
    q80 = out["known_at_ts"].quantile(0.80)
    out["temporal_split"] = np.where(
        out["known_at_ts"] <= q60,
        "discovery",
        np.where(out["known_at_ts"] <= q80, "validation", "oos"),
    )
    splits = {
        "method": "chronological_known_at_quantiles_60_20_20_on_analyzable_r6_episodes",
        "n_total": int(len(out)),
        "discovery_end_known_at": str(q60),
        "validation_end_known_at": str(q80),
        "n_discovery": int((out["temporal_split"] == "discovery").sum()),
        "n_validation": int((out["temporal_split"] == "validation").sum()),
        "n_oos": int((out["temporal_split"] == "oos").sum()),
        "fixed_before_rule_selection": True,
        "note": "V2 splits not reused when OI/liq coverage starts later; core OB/trades/candles drive analyzability",
    }
    dmax = out.loc[out["temporal_split"] == "discovery", "known_at_ts"].max()
    vmin = out.loc[out["temporal_split"] == "validation", "known_at_ts"].min()
    vmax = out.loc[out["temporal_split"] == "validation", "known_at_ts"].max()
    omin = out.loc[out["temporal_split"] == "oos", "known_at_ts"].min()
    splits["nonoverlap_ok"] = bool(
        (pd.isna(vmin) or dmax <= vmin) and (pd.isna(omin) or vmax <= omin)
    )
    return out, splits


def strength_decay_row(ep: pd.Series, members_meta: pd.DataFrame) -> dict[str, Any]:
    """Causal strength state using only components known at known_at / first touch."""
    known = pd.Timestamp(ep["known_at"])
    ft = pd.Timestamp(ep["first_touch_at"]) if pd.notna(ep.get("first_touch_at")) else known
    mid = str(ep.get("member_ids") or "")
    ids = [x for x in mid.split("|") if x]
    # member meta: entity_id, known_at, strength, invalidated optional — from leaders we only have episode members as ids
    # Use n_components and strength_at_known as baseline; reinforcement if more cluster members known later but BEFORE touch
    out = {
        "episode_id": ep["episode_id"],
        "strength_at_known": ep.get("strength_at_known"),
        "age_minutes_at_touch": None
        if pd.isna(ft)
        else (ft - known).total_seconds() / 60.0,
        "n_components_at_known": ep.get("n_components"),
        "new_components_before_touch": None,
        "strength_reinforcement_flag": None,
        "strength_decay_flag": None,
        "prior_tests": None,
        "fresh_cluster_flag": None,
        "old_untested_flag": None,
    }
    if members_meta is not None and len(members_meta) and ids:
        sub = members_meta[members_meta["entity_id"].isin(ids)].copy()
        if len(sub):
            sub["known_at_ts"] = pd.to_datetime(sub["known_at"]).dt.tz_localize(None)
            at_known = sub[sub["known_at_ts"] <= known]
            before_touch = sub[sub["known_at_ts"] <= ft]
            out["n_components_at_known"] = max(int(ep.get("n_components") or 0), len(at_known))
            out["new_components_before_touch"] = max(0, len(before_touch) - len(at_known))
            out["strength_reinforcement_flag"] = out["new_components_before_touch"] > 0
            # decay proxy: if strength missing later — not available without live state; use age
            age = out["age_minutes_at_touch"] or 0
            out["fresh_cluster_flag"] = age <= 30
            out["old_untested_flag"] = age >= 120
            out["strength_decay_flag"] = age >= 180 and not out["strength_reinforcement_flag"]
    else:
        age = out["age_minutes_at_touch"] or 0
        out["fresh_cluster_flag"] = age <= 30
        out["old_untested_flag"] = age >= 120
        out["strength_decay_flag"] = age >= 180
        out["new_components_before_touch"] = 0
        out["strength_reinforcement_flag"] = False
    return out


def run_phase3(
    *,
    out_dir: Path = DEFAULT_OUT,
    v2_dir: Path = DEFAULT_V2,
    v1_dir: Path = DEFAULT_V1,
    primary_t3_sec: int = PRIMARY_T3_SEC,
    n_boot: int = 200,
) -> dict[str, Any]:
    t_start = time.time()
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    print("R6_PARITY", flush=True)
    episodes, parity = build_r6_episodes(v2_dir=v2_dir, v1_dir=v1_dir)
    print(
        f"  episodes={len(episodes)} parity_equal={parity['episode_id_set_equal']}",
        flush=True,
    )
    if not parity["episode_id_set_equal"] or len(episodes) == 0:
        verdict = VERDICT_COVERAGE_BLOCKED
        _write_blocked(out_dir, verdict, parity, "R6 parity failed")
        return {"verdict": verdict, "parity": parity}

    client = get_clickhouse_client()
    raw_probe = probe_raw_ob200_available(client)
    print("RAW_OB_PROBE", raw_probe, flush=True)

    # preload market data per symbol spanning episode range
    episodes["known_at_ts"] = pd.to_datetime(episodes["known_at"])
    episodes["first_touch_ts"] = pd.to_datetime(episodes["first_touch_at"])
    episodes["approach_ts"] = pd.to_datetime(episodes["approach_at"])
    tmin = episodes["known_at_ts"].min() - pd.Timedelta(hours=2)
    tmax = episodes["first_touch_ts"].max() + pd.Timedelta(hours=2)

    market: dict[str, dict[str, pd.DataFrame]] = {}
    for sym in sorted(episodes["symbol"].unique()):
        print(f"LOAD_MARKET {sym}", flush=True)
        market[sym] = {
            "ob": fetch_ob_agg_1s(client, sym, tmin.to_pydatetime(), tmax.to_pydatetime()),
            "trades": fetch_trades_1s(client, sym, tmin.to_pydatetime(), tmax.to_pydatetime()),
            "oi": fetch_oi_5s(client, sym, tmin.to_pydatetime(), tmax.to_pydatetime()),
            "liq": fetch_liquidations(client, sym, tmin.to_pydatetime(), tmax.to_pydatetime()),
            "candles": fetch_candles_1m(client, sym, tmin.to_pydatetime(), tmax.to_pydatetime()),
        }
        print(
            f"  ob={len(market[sym]['ob'])} trades={len(market[sym]['trades'])} "
            f"oi={len(market[sym]['oi'])} liq={len(market[sym]['liq'])} candles={len(market[sym]['candles'])}",
            flush=True,
        )

    # EMA context from v1
    ema = pd.read_csv(v1_dir / "pool_ema_context.csv", low_memory=False)
    ema_touch = ema[ema["label"] == "FIRST_TOUCH"].drop_duplicates("entity_id")
    ema_created = ema[ema["label"] == "CREATED"].drop_duplicates("entity_id")

    # member meta for strength
    ent = pd.read_csv(
        v2_dir / "entity_enriched.csv",
        usecols=["entity_id", "known_at", "n_components"],
        low_memory=False,
    )
    members_meta = ent.copy()

    cov_rows = []
    cp_rows = []
    ob_rows = []
    tr_rows = []
    oi_rows = []
    sd_rows = []
    ema_rows = []
    label_rows = []
    feat_rows = []

    for _, ep in episodes.iterrows():
        sym = ep["symbol"]
        mkt = market[sym]
        t0 = ep["known_at_ts"]
        t1 = ep["approach_ts"]
        t2 = ep["first_touch_ts"]
        cov = coverage_for_episode(
            symbol=sym,
            t0=t0,
            t1=t1,
            t2=t2,
            ob=mkt["ob"],
            trades=mkt["trades"],
            oi=mkt["oi"],
            liq=mkt["liq"],
            candles=mkt["candles"],
        )
        analyzable = bool(cov["analyzable_core"]) and pd.notna(t2)
        status = "ANALYZABLE" if analyzable else "NOT_ANALYZABLE"
        cov_rows.append(
            {
                "episode_id": ep["episode_id"],
                "symbol": sym,
                "side": ep["side"],
                "timeframe": ep["timeframe"],
                "status": status,
                "analyzable_core": analyzable,
                "oi_available": cov["oi_available"],
                "liq_available": cov["liq_available"],
                "ob_source_kind": cov["ob_source_kind"],
                "ob_per_level_raw": cov["ob_per_level_raw"],
                "pre_ob_status": cov["pre_approach"]["ob"]["status"],
                "pre_trades_status": cov["pre_approach"]["trades"]["status"],
                "touch_ob_status": cov["at_first_touch"]["ob"]["status"],
                "touch_trades_status": cov["at_first_touch"]["trades"]["status"],
                "post_ob_status": cov["post_touch_60s"]["ob"]["status"],
                "post_trades_status": cov["post_touch_60s"]["trades"]["status"],
                "pre_ob_gap_frac": cov["pre_approach"]["ob"]["gap_frac"],
                "touch_ob_gap_frac": cov["at_first_touch"]["ob"]["gap_frac"],
                "post_ob_gap_frac": cov["post_touch_60s"]["ob"]["gap_frac"],
                "liq_note": cov.get("liq_note"),
            }
        )

        # checkpoints for all T3 windows
        for sec in T3_WINDOWS_SEC:
            cp_rows.append(build_checkpoint_row(ep, sec, None))
        for m in T3_WINDOWS_1M:
            cp_rows.append(build_checkpoint_row(ep, None, m))

        label_rows.append(
            {
                "episode_id": ep["episode_id"],
                "label_primary": ep["label_primary"],
                "defended": ep["defended"],
                "swept_reclaimed": ep["swept_reclaimed"],
                "consumed_accepted": ep["consumed_accepted"],
                "status": status,
                "data_gap": not analyzable,
                "unresolved": ep["label_primary"] == "unresolved",
                "not_analyzable": not analyzable,
            }
        )

        sd_rows.append(strength_decay_row(ep, members_meta))

        # EMA
        lead = ep["leader_entity_id"]
        ec = ema_created[ema_created["entity_id"] == lead]
        et = ema_touch[ema_touch["entity_id"] == lead]
        er = {"episode_id": ep["episode_id"], "leader_entity_id": lead}
        if len(ec):
            r = ec.iloc[0]
            er.update(
                {
                    "ema_regime_created": r.get("ema_regime"),
                    "pool_vs_ema200": r.get("pool_vs_ema200"),
                    "pool_between_ema20_59": r.get("pool_between_ema20_59"),
                    "ema20_slope_created": r.get("ema20_slope"),
                    "ema59_slope_created": r.get("ema59_slope"),
                }
            )
        if len(et):
            r = et.iloc[0]
            er.update(
                {
                    "ema_regime_touch": r.get("ema_regime"),
                    "touch_ema_with_bar": r.get("touch_ema_with_bar"),
                }
            )
        ema_rows.append(er)

        if not analyzable:
            continue

        # feature windows relative to T2 / T1
        t_pre_b = t1 if pd.notna(t1) else t2
        t_pre_a = t_pre_b - pd.Timedelta(minutes=5)
        t_touch_a = t2 - pd.Timedelta(seconds=5)
        t_touch_b = t2 + pd.Timedelta(seconds=5)
        t3 = t2 + pd.Timedelta(seconds=primary_t3_sec)
        t_post_a = t2
        t_post_b = t3

        obf = extract_ob_features(
            mkt["ob"],
            side=ep["side"],
            lower=float(ep["lower_price"]),
            upper=float(ep["upper_price"]),
            t_pre_a=t_pre_a,
            t_pre_b=t_pre_b,
            t_touch_a=t_touch_a,
            t_touch_b=t_touch_b,
            t_post_a=t_post_a,
            t_post_b=t_post_b,
        )
        obf["episode_id"] = ep["episode_id"]
        obf["decision_at"] = t3.isoformat()
        obf["decision_window"] = f"T3_{primary_t3_sec}s"
        ob_rows.append(obf)

        trf = extract_trade_features(
            mkt["trades"],
            mkt["candles"],
            side=ep["side"],
            t_pre_a=t_pre_a,
            t_pre_b=t_pre_b,
            t_touch_a=t_touch_a,
            t_touch_b=t_touch_b,
            t_post_a=t_post_a,
            t_post_b=t_post_b,
        )
        trf["episode_id"] = ep["episode_id"]
        trf["decision_at"] = t3.isoformat()
        tr_rows.append(trf)

        oif = extract_oi_liq_features(
            mkt["oi"],
            mkt["liq"],
            side=ep["side"],
            t_pre_a=t_pre_a,
            t_pre_b=t_pre_b,
            t_touch_a=t_touch_a,
            t_post_b=t_post_b,
        )
        oif["episode_id"] = ep["episode_id"]
        oi_rows.append(oif)

        erf = edge_reclaim_features(
            mkt["candles"],
            side=ep["side"],
            lower=float(ep["lower_price"]),
            upper=float(ep["upper_price"]),
            t2=t2,
            t3=t3,
        )

        # merged feature row for rules
        feat = {
            "episode_id": ep["episode_id"],
            "symbol": ep["symbol"],
            "side": ep["side"],
            "timeframe": ep["timeframe"],
            "known_at": ep["known_at"],
            "known_at_ts": t0,
            "utc_day": t0.strftime("%Y-%m-%d"),
            "label_primary": ep["label_primary"],
            "decision_at": t3.isoformat(),
            "decision_window_sec": primary_t3_sec,
            **{k: obf.get(k) for k in [
                "depth_replenishment_flag",
                "depth_depletion_flag",
                "book_flip_toward_defense",
                "wall_persistence_proxy",
                "post_net_replenishment",
                "pre_pool_depth_mean",
                "post_pool_depth_mean",
            ]},
            **{k: trf.get(k) for k in [
                "impact_compression_flag",
                "flow_flip_flag",
                "flow_deceleration_flag",
                "absorption_flag",
                "touch_agg_hit_notional",
                "post_delta_notional",
            ]},
            **{k: oif.get(k) for k in [
                "oi_status",
                "liq_status",
                "oi_drop_on_sweep",
                "liq_flush_toward_pool",
                "liq_burst_flag",
                "oi_change_frac",
            ]},
            **erf,
        }
        feat_rows.append(feat)

    cov_df = pd.DataFrame(cov_rows)
    n_analyzable = int(cov_df["analyzable_core"].sum())
    print(f"ANALYZABLE {n_analyzable}/{len(episodes)}", flush=True)

    # attach analyzable flag to episodes
    episodes = episodes.merge(cov_df[["episode_id", "status", "analyzable_core", "oi_available", "liq_available"]], on="episode_id", how="left")

    if n_analyzable < 30:
        verdict = VERDICT_COVERAGE_BLOCKED
        _write_partial(
            out_dir,
            verdict,
            episodes,
            parity,
            raw_probe,
            cov_df,
            pd.DataFrame(cp_rows),
            pd.DataFrame(label_rows),
        )
        return {"verdict": verdict, "n_analyzable": n_analyzable, "parity": parity}

    feat_df = pd.DataFrame(feat_rows)
    feat_df, splits = assign_coverage_splits(feat_df)

    # baselines
    r6_def_base = float((feat_df["label_primary"] == "DEFENDED").mean())
    # V2 single baseline frozen
    single_base = 0.059

    print("RULES", flush=True)
    candidates, oos_results, selected = evaluate_rules(
        feat_df,
        r6_defense_baseline=r6_def_base,
        single_defense_baseline=single_base,
        n_boot=n_boot,
    )
    confirmed = oos_results[oos_results["confirmed_oos"] == True]  # noqa: E712

    # optional model smoke
    model_note = _model_smoke(feat_df)

    # bootstrap on label rates among analyzable
    boot_rows = []
    for label, col in [
        ("analyzable_defended", "DEFENDED"),
        ("analyzable_sweep_reclaim", "SWEPT_RECLAIMED"),
        ("analyzable_consumed", "CONSUMED_ACCEPTED"),
    ]:
        tmp = feat_df.assign(_hit=feat_df["label_primary"] == col)
        br = block_bootstrap_rate(tmp, success_col="_hit", block_cols=["utc_day"], n_boot=n_boot)
        boot_rows.append({"cohort": label, **br})
    bootstrap = pd.DataFrame(boot_rows)

    quality = (
        feat_df.groupby(["symbol", "timeframe", "side", "temporal_split"], dropna=False)
        .agg(
            n=("episode_id", "count"),
            defense_rate=("label_primary", lambda s: float((s == "DEFENDED").mean())),
            reclaim_rate=("label_primary", lambda s: float((s == "SWEPT_RECLAIMED").mean())),
            consume_rate=("label_primary", lambda s: float((s == "CONSUMED_ACCEPTED").mean())),
        )
        .reset_index()
    )

    # write artifacts
    _write_csv(out_dir / "r6_episodes.csv", episodes)
    _write_csv(out_dir / "coverage_by_episode.csv", cov_df)
    _write_csv(out_dir / "causal_checkpoints.csv", pd.DataFrame(cp_rows))
    _write_csv(out_dir / "orderbook_features.csv", pd.DataFrame(ob_rows))
    _write_csv(out_dir / "public_trade_features.csv", pd.DataFrame(tr_rows))
    _write_csv(out_dir / "oi_liquidation_features.csv", pd.DataFrame(oi_rows))
    _write_csv(out_dir / "strength_decay_features.csv", pd.DataFrame(sd_rows))
    _write_csv(out_dir / "ema_context.csv", pd.DataFrame(ema_rows))
    _write_csv(out_dir / "labels.csv", pd.DataFrame(label_rows))
    _write_csv(out_dir / "rule_candidates.csv", candidates)
    _write_csv(out_dir / "oos_results.csv", oos_results)
    _write_csv(out_dir / "bootstrap_intervals.csv", bootstrap)
    _write_csv(out_dir / "quality_by_symbol.csv", quality)
    _write_csv(out_dir / "feature_matrix_t3.csv", feat_df)

    (out_dir / "temporal_splits.json").write_text(json.dumps(splits, indent=2, default=str), encoding="utf-8")
    write_feature_dictionary(out_dir)
    write_methodology(out_dir, raw_probe)

    if len(confirmed):
        verdict = VERDICT_COMPLETE
    else:
        verdict = VERDICT_NO_STABLE

    # If raw L2 required strictly — we still complete with aggregate-only caveat
    manifest = {
        "analysis_id": ANALYSIS_ID,
        "version": ANALYSIS_VERSION,
        "verdict": verdict,
        "r6_contract": R6_CONTRACT,
        "parity": parity,
        "raw_ob200_probe": raw_probe,
        "n_r6_episodes": int(len(episodes)),
        "n_analyzable": n_analyzable,
        "n_not_analyzable": int(len(episodes) - n_analyzable),
        "primary_t3_sec": primary_t3_sec,
        "splits": splits,
        "n_rules_selected_discovery": len(selected),
        "n_rules_confirmed_oos": int(len(confirmed)),
        "r6_defense_baseline_analyzable": r6_def_base,
        "single_defense_baseline_frozen": single_base,
        "model_smoke": model_note,
        "ob_analysis_mode": "AGGREGATE_PROXY_ONLY",
        "created_at": pd.Timestamp.utcnow().isoformat(),
        "elapsed_sec": round(time.time() - t_start, 2),
        "no_commit": True,
        "no_bot_pnl": True,
        "no_clickhouse_writes": True,
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")
    write_report(out_dir, manifest, episodes, cov_df, candidates, oos_results, confirmed, bootstrap, model_note)
    print("VERDICT", verdict, flush=True)
    return {"verdict": verdict, "manifest": manifest, "out_dir": str(out_dir)}


def _model_smoke(feat_df: pd.DataFrame) -> dict[str, Any]:
    """Optional logistic + shallow tree; leak-aware time splits."""
    note: dict[str, Any] = {"ran": False}
    try:
        from sklearn.linear_model import LogisticRegression
        from sklearn.tree import DecisionTreeClassifier
        from sklearn.metrics import roc_auc_score
    except Exception as exc:  # noqa: BLE001
        return {"ran": False, "reason": f"sklearn_unavailable:{exc}"}

    cols = [
        "depth_replenishment_flag",
        "depth_depletion_flag",
        "flow_flip_flag",
        "impact_compression_flag",
        "near_edge_reclaim",
        "absorption_flag",
        "book_flip_toward_defense",
        "wall_persistence_proxy",
    ]
    use = feat_df.dropna(subset=["label_primary"]).copy()
    # binary: DEFENDED vs rest
    use["y"] = (use["label_primary"] == "DEFENDED").astype(int)
    X = use[cols].fillna(False).astype(int)
    if len(use) < 80 or use["y"].sum() < 10:
        return {"ran": False, "reason": "insufficient_n"}
    disc = use["temporal_split"] == "discovery"
    oos = use["temporal_split"] == "oos"
    if disc.sum() < 40 or oos.sum() < 15:
        return {"ran": False, "reason": "insufficient_split_n"}
    try:
        lr = LogisticRegression(max_iter=500, class_weight="balanced")
        lr.fit(X.loc[disc], use.loc[disc, "y"])
        proba = lr.predict_proba(X.loc[oos])[:, 1]
        auc = float(roc_auc_score(use.loc[oos, "y"], proba)) if use.loc[oos, "y"].nunique() > 1 else None
        tree = DecisionTreeClassifier(max_depth=3, class_weight="balanced", random_state=0)
        tree.fit(X.loc[disc], use.loc[disc, "y"])
        auc_t = float(roc_auc_score(use.loc[oos, "y"], tree.predict_proba(X.loc[oos])[:, 1])) if use.loc[oos, "y"].nunique() > 1 else None
        note = {
            "ran": True,
            "target": "DEFENDED_vs_rest",
            "features": cols,
            "logistic_oos_auc": auc,
            "tree_depth3_oos_auc": auc_t,
            "note": "Smoke only; not a trading model. Compare to transparent rules.",
        }
    except Exception as exc:  # noqa: BLE001
        note = {"ran": False, "reason": str(exc)}
    return note


def _write_blocked(out_dir: Path, verdict: str, parity: dict, msg: str) -> None:
    (out_dir / "manifest.json").write_text(
        json.dumps({"verdict": verdict, "parity": parity, "error": msg, "r6_contract": R6_CONTRACT}, indent=2),
        encoding="utf-8",
    )
    (out_dir / "report.md").write_text(f"# {verdict}\n\n{msg}\n", encoding="utf-8")


def _write_partial(out_dir, verdict, episodes, parity, raw_probe, cov_df, cp_df, labels) -> None:
    _write_csv(out_dir / "r6_episodes.csv", episodes)
    _write_csv(out_dir / "coverage_by_episode.csv", cov_df)
    _write_csv(out_dir / "causal_checkpoints.csv", cp_df)
    _write_csv(out_dir / "labels.csv", labels)
    for name in [
        "orderbook_features.csv",
        "public_trade_features.csv",
        "oi_liquidation_features.csv",
        "strength_decay_features.csv",
        "ema_context.csv",
        "rule_candidates.csv",
        "oos_results.csv",
        "bootstrap_intervals.csv",
        "quality_by_symbol.csv",
    ]:
        _write_csv(out_dir / name, pd.DataFrame())
    (out_dir / "temporal_splits.json").write_text("{}", encoding="utf-8")
    write_feature_dictionary(out_dir)
    write_methodology(out_dir, raw_probe)
    manifest = {
        "verdict": verdict,
        "parity": parity,
        "raw_ob200_probe": raw_probe,
        "n_analyzable": int(cov_df["analyzable_core"].sum()),
        "r6_contract": R6_CONTRACT,
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")
    (out_dir / "report.md").write_text(
        f"# {verdict}\n\nInsufficient analyzable R6 episodes for orderflow confirmation.\n"
        f"analyzable={int(cov_df['analyzable_core'].sum())}/{len(episodes)}\n"
        f"raw_ob={raw_probe}\n",
        encoding="utf-8",
    )


def write_feature_dictionary(out_dir: Path) -> None:
    text = """# Feature dictionary — R6 orderflow confirmation V1

## OB source
- **AGGREGATE_PROXY** from `orderbook_analysis.orderbook_features_1s_v2` (parser ob200_v3, depth=200).
- **Not** genuine per-level raw OB200. `orderbook_deltas` unavailable on this host.
- Never interpret aggregate fields as queue-position L2 truth.

## Orderbook (aggregate)
- pool_depth / opp_depth: bid_qty_l50 vs ask_qty_l50 mirrored by side
- imbalance_l50, spread_bps, ofi, mid_price_change
- depth_added/removed, cancel_to_add, net_replenishment
- wall_* proxies from aggregate wall fields
- depth_replenishment_flag / depletion_flag / book_flip / wall_persistence_proxy

## Public trades
- agg_hit_notional: sells into BID / buys into ASK
- delta_notional, trades_per_sec, notional_per_sec, large_trade_sec_share
- impact_per_agg, price_continuation, absorption, impact_compression, flow_flip, flow_deceleration

## OI / liquidations
- Missing/empty slices are **not** zeros (`oi_status`, `liq_status`, notes).
- oi_drop_on_sweep, liq_flush_toward_pool only when status=VALID.

## Strength decay
- age_minutes_at_touch, fresh/old flags, reinforcement if new members known before touch.
- No future components attached retroactively.

## EMA
- Context only from v1 CREATED/FIRST_TOUCH snapshots.

## Labels
- DEFENDED / SWEPT_RECLAIMED / CONSUMED_ACCEPTED from V2 primary variant (acc=2, reclaim=6, react=0.5).
- not_analyzable / data_gap / unresolved as meta statuses.
"""
    path = out_dir / "feature_dictionary.md"
    if path.exists():
        raise FileExistsError(path)
    path.write_text(text, encoding="utf-8")


def write_methodology(out_dir: Path, raw_probe: dict) -> None:
    text = f"""# Methodology — LIQUIDITY_LOCATION_R6_ORDERFLOW_CONFIRMATION_V1

## R6 contract (frozen)
{json.dumps(R6_CONTRACT, indent=2)}

## Parity
Episodes = unique `episode_id` of V2 entities matching R6 mask.
Leader entity = max n_components then earliest known_at; outcomes from V2 primary variant.

## OB mode
Raw probe: {json.dumps(raw_probe, default=str)}
Analysis uses aggregate proxy only when per-level raw is unavailable.

## Checkpoints
T0 known_at, T1 approach, T2 first touch, T3 decision (1/3/5/15/30/60s and 1/3 closed 1m), T4 label.
Primary rule features use T3=30s after first touch. Features use only data ≤ T3.

## Splits
Chronological 60/20/20 on analyzable episodes by known_at. Rules selected in Discovery only.

## Safety
No commit, no CH writes, no bot/PnL, no overwrite of prior result folders.
"""
    path = out_dir / "methodology.md"
    if path.exists():
        raise FileExistsError(path)
    path.write_text(text, encoding="utf-8")


def write_report(
    out_dir: Path,
    manifest: dict,
    episodes: pd.DataFrame,
    cov: pd.DataFrame,
    candidates: pd.DataFrame,
    oos: pd.DataFrame,
    confirmed: pd.DataFrame,
    bootstrap: pd.DataFrame,
    model_note: dict,
) -> None:
    v = manifest["verdict"]
    lines = [
        f"# {v}",
        "",
        "## 1. VERDICT",
        v,
        "",
        "## 2. LIVE-SICHERHEIT",
        "- No commit, no dashboard restart, no collector change, no CH writes, no bot/PnL.",
        "",
        "## 3. R6-PARITÄT",
        json.dumps(manifest.get("parity"), indent=2, default=str),
        "",
        "## 4. DATENCOVERAGE",
        f"- Raw OB200 per-level: {manifest.get('raw_ob200_probe')}",
        f"- OB mode: {manifest.get('ob_analysis_mode')}",
        f"- analyzable: {manifest.get('n_analyzable')} / {manifest.get('n_r6_episodes')}",
        f"- not_analyzable: {manifest.get('n_not_analyzable')}",
        "",
        "## 5. ANALYSIERBARE EPISODEN",
        f"- n={manifest.get('n_analyzable')}",
        f"- label mix: {episodes.loc[episodes.get('analyzable_core')==True, 'label_primary'].value_counts().to_dict() if 'analyzable_core' in episodes.columns else 'n/a'}",
        "",
        "## 6. KAUSALE CHECKPOINTS",
        "See causal_checkpoints.csv (T0–T4; T3 windows 1s…60s and 1m/3m closed).",
        f"Primary decision window: T3={manifest.get('primary_t3_sec')}s",
        "",
        "## 7. ORDERBOOK",
        "Aggregate proxy features only (depth replenish/depletion, imbalance flip, wall persistence).",
        "See orderbook_features.csv — not genuine L2 queues.",
        "",
        "## 8. PUBLIC TRADES",
        "Mirrored hit aggression, impact compression, absorption, flow flip. See public_trade_features.csv.",
        "",
        "## 9. OI/LIQUIDATIONEN",
        "Used only when status=VALID; empty slices not treated as zero. See oi_liquidation_features.csv.",
        "",
        "## 10. STRENGTH-DECAY",
        "Age / fresh / old / reinforcement flags. See strength_decay_features.csv.",
        "",
        "## 11. EMA-KONTEXT",
        "Context only (no forced EMA trigger). See ema_context.csv.",
        "",
        "## 12. TRANSPARENTE REGELN",
    ]
    if len(candidates):
        cols = [
            c
            for c in [
                "rule_id",
                "predicts",
                "discovery_n",
                "discovery_precision",
                "oos_n",
                "oos_precision",
                "oos_lift_vs_r6_defense_baseline",
                "selected_on_discovery",
            ]
            if c in candidates.columns
        ]
        lines.append(candidates[cols].to_string(index=False))
    lines += ["", "## 13. TEMPORALES OOS", json.dumps(manifest.get("splits"), indent=2, default=str)]
    lines += ["", "## 14. OPTIONALER MODELL-SMOKE", json.dumps(model_note, indent=2, default=str)]
    lines += ["", "## 15. STABILE BESTÄTIGUNGSPATTERNS"]
    if len(confirmed):
        lines.append(confirmed.to_string(index=False))
    else:
        lines.append("None under pre-registered OOS criteria.")
    lines += ["", "## 16. VERWORFENE SCHEINPATTERNS"]
    if len(oos):
        for _, r in oos[oos["confirmed_oos"] == False].iterrows():  # noqa: E712
            lines.append(f"- {r['rule_id']}: {r['reason']}")
    lines += [
        "",
        "## 17. BLOCKER",
        f"- Per-level raw OB200: unavailable ({manifest.get('raw_ob200_probe')})",
        "- Aggregate proxy used explicitly; wall/queue claims limited.",
        "",
        "## 18. EMPFEHLUNG FÜR PHASE 4",
        "1) Restore genuine OB200 per-level feed before claiming wall/queue edges.",
        "2) If a transparent rule OOS-confirms on aggregate: observational checklist only.",
        "3) Expand analyzable window once OI/liq coverage is continuous.",
        f"",
        f"Elapsed: {manifest.get('elapsed_sec')}s",
        "",
        "### Bootstrap",
        bootstrap.to_string(index=False) if len(bootstrap) else "n/a",
    ]
    path = out_dir / "report.md"
    if path.exists():
        raise FileExistsError(path)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

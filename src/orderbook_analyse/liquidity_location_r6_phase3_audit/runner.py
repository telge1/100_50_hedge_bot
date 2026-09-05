"""Run Phase-3 leakage + raw-OB inventory audit (read-only)."""

from __future__ import annotations

import json
import math
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from orderbook_analyse.cluster_sweep_research.clickhouse_source import (
    aggregate_timeframe,
    fetch_candles_1m,
)
from orderbook_analyse.liquidity_location_pool_edge_validation_v2.stats import (
    block_bootstrap_rate,
    wilson_interval,
)
from orderbook_analyse.ob200_v3_raw_discovery.files import (
    excluded_tmp_files,
    list_closed_segments,
)
from orderbook_analyse.orderbook_v2.ch_client import get_clickhouse_client

from . import (
    AUDIT_ID,
    LIVE_ARCHIVE_DEFAULT,
    OUT_DIR_DEFAULT,
    PHASE3_DIR_DEFAULT,
    PRIMARY_T3_SEC,
    SHADOW_ARCHIVE,
    VERDICT_COMPLETE,
    VERDICT_LEAKAGE,
    VERDICT_LOADER_BROKEN,
)

# Frozen definitions (documented in methodology)
NEAR_EDGE_RECLAIM_DEF = {
    "name": "near_edge_reclaim",
    "window": "[first_touch_at, decision_at=T3) on 1m open_time",
    "price_rule": "BID: any 1m close > pool.upper (near edge); ASK: close < pool.lower",
    "reaction_distance": "none (binary edge reclaim only)",
    "data": "1m candle closes for bars whose open_time is in [T2, T3)",
    "availability_bug": (
        "For T3=30s the included bar typically opens at T2; its close is only known at T2+1m > T3. "
        "Phase-3 treated the feature as available at T3 → close leakage."
    ),
}
DEFENDED_DEF = {
    "name": "DEFENDED",
    "window": "from analysis_start after known_at until/without sweep; reaction after first touch",
    "rule": (
        "touched, never SWEPT (far edge not fully traversed), then price moves away from "
        "near edge by reaction_atr_mult * ATR (primary variant 0.5 ATR)"
    ),
    "reaction_distance": "0.5 ATR (primary Phase-3/V2 variant)",
    "exclusive_vs": "SWEPT / SWEPT_RECLAIMED / CONSUMED_ACCEPTED",
}
SWEPT_RECLAIMED_DEF = {
    "name": "SWEPT_RECLAIMED",
    "window": "after full far-edge sweep; reclaim within reclaim_horizon_bars (primary=6)",
    "rule": "after SWEPT, close back beyond near edge within horizon",
}
ABSORPTION_DEF = {
    "name": "absorption_flag",
    "feature_window_trades": "[T2-5s, T2+5s)",
    "price_continuation_candle_window_phase3": "[T2-5s, T2+5s+1m)  # LEAKAGE: extends past T3=30s",
    "rule": "agg_hit_notional>0 AND price_continuation < 0.0005 in touch window",
    "availability": "trades up to T2+5s OK for T3=30s; candle impact used through T2+65s — AFTER T3",
}


def _ts(x: Any) -> pd.Timestamp:
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return pd.NaT
    t = pd.Timestamp(x)
    if t.tzinfo is not None:
        t = t.tz_convert("UTC").tz_localize(None)
    return t


def _write_csv(path: Path, df: pd.DataFrame) -> None:
    if path.exists():
        raise FileExistsError(path)
    df.to_csv(path, index=False)


def _first_close_reclaim_at(
    candles: pd.DataFrame, *, side: str, near: float, start: pd.Timestamp, end: pd.Timestamp
) -> tuple[pd.Timestamp, pd.Timestamp]:
    """Return (bar_open_used_by_phase3, close_known_at).

    Phase-3 includes 1m bars with open_time in [T2, T3) and reads their *close*.
    For T3=30s that is typically the bar opening at T2; its close is only known at T2+1m
    (> decision_at) — close leakage.
    """
    if candles.empty or pd.isna(start) or pd.isna(end):
        return pd.NaT, pd.NaT
    sl = candles[(candles["open_time"] >= start) & (candles["open_time"] < end)]
    for _, r in sl.iterrows():
        c = float(r["close"])
        ok = c > near if side == "BID" else c < near
        if ok:
            open_t = pd.Timestamp(r["open_time"])
            return open_t, open_t + pd.Timedelta(minutes=1)
    return pd.NaT, pd.NaT


def _outcome_resolved_at(ep: pd.Series) -> tuple[pd.Timestamp, str]:
    """Earliest time when Phase-3 primary label becomes determined."""
    label = str(ep.get("label_primary") or "unresolved")
    if label == "DEFENDED":
        t = _ts(ep.get("defend_at"))
        return t, "defend_at"
    if label == "SWEPT_RECLAIMED":
        t = _ts(ep.get("reclaim_at"))
        return t, "reclaim_at"
    if label == "CONSUMED_ACCEPTED":
        # acceptance ends after K bars from sweep — use reclaim/defend empty; approximate sweep + horizon
        sw = _ts(ep.get("sweep_at"))
        if pd.isna(sw):
            return pd.NaT, "missing_sweep"
        tf = str(ep.get("timeframe") or "15m")
        tfm = 5 if tf.startswith("5") else 15 if tf.startswith("15") else 30
        # primary acceptance_bars=2
        return sw + pd.Timedelta(minutes=tfm * 2), "sweep_at+2bars"
    if label == "unresolved":
        # typically SWEPT without reclaim/accept — resolved at sweep
        sw = _ts(ep.get("sweep_at"))
        if pd.notna(sw):
            return sw, "sweep_at_unresolved_swept"
        return pd.NaT, "unresolved_open"
    return pd.NaT, "unknown_label"


def build_decision_timestamps(
    episodes: pd.DataFrame,
    feat: pd.DataFrame,
    candles_by_sym: dict[str, pd.DataFrame],
    *,
    t3_sec: int = PRIMARY_T3_SEC,
) -> pd.DataFrame:
    rows = []
    fidx = feat.set_index("episode_id") if len(feat) and "episode_id" in feat.columns else None
    for _, ep in episodes.iterrows():
        eid = ep["episode_id"]
        t2 = _ts(ep.get("first_touch_at"))
        t3 = t2 + pd.Timedelta(seconds=t3_sec) if pd.notna(t2) else pd.NaT
        side = str(ep["side"])
        near = float(ep["upper_price"]) if side == "BID" else float(ep["lower_price"])
        candles = candles_by_sym.get(ep["symbol"], pd.DataFrame())
        reclaim_open, reclaim_known = _first_close_reclaim_at(
            candles, side=side, near=near, start=t2, end=t3
        )
        phase3_reclaim = None
        abs_flag = None
        if fidx is not None and eid in fidx.index:
            rowf = fidx.loc[eid]
            if isinstance(rowf, pd.DataFrame):
                rowf = rowf.iloc[0]
            if "near_edge_reclaim" in getattr(rowf, "index", []):
                phase3_reclaim = rowf.get("near_edge_reclaim")
            if "absorption_flag" in getattr(rowf, "index", []):
                abs_flag = rowf.get("absorption_flag")
        abs_at = t2 + pd.Timedelta(seconds=5) if abs_flag is True and pd.notna(t2) else pd.NaT
        # Phase-3 absorption also used candles to T2+65s — record that leakage end
        abs_feature_end_phase3 = (
            t2 + pd.Timedelta(seconds=5) + pd.Timedelta(minutes=1) if pd.notna(t2) else pd.NaT
        )

        resolved_at, resolved_src = _outcome_resolved_at(ep)
        label_horizon_end = t2 + pd.Timedelta(hours=24) if pd.notna(t2) else pd.NaT

        timing = "unknown"
        if pd.isna(resolved_at):
            timing = "unresolved_through_horizon"
        elif pd.isna(t3):
            timing = "missing_t3"
        elif resolved_at < t3:
            timing = "outcome_before_t3"
        elif resolved_at == t3:
            timing = "outcome_at_t3"
        else:
            timing = "outcome_after_t3"

        usable_predict = timing == "outcome_after_t3"
        # Phase-3 treated reclaim as available at T3 if any bar open in [T2,T3) reclaimed.
        reclaim_flag = bool(phase3_reclaim) if phase3_reclaim is not None else bool(pd.notna(reclaim_open))
        reclaim_close_leaks = bool(
            pd.notna(reclaim_known) and pd.notna(t3) and reclaim_known > t3
        )
        definitional_overlap = bool(ep.get("label_primary") == "DEFENDED" and reclaim_flag)

        rows.append(
            {
                "episode_id": eid,
                "symbol": ep["symbol"],
                "timeframe": ep["timeframe"],
                "side": side,
                "analyzable_core": bool(ep.get("analyzable_core")),
                "label_primary": ep.get("label_primary"),
                "first_touch_at": None if pd.isna(t2) else t2.isoformat(),
                "decision_at": None if pd.isna(t3) else t3.isoformat(),
                "near_edge_reclaim_at": None if pd.isna(reclaim_open) else reclaim_open.isoformat(),
                "near_edge_reclaim_close_known_at": None
                if pd.isna(reclaim_known)
                else reclaim_known.isoformat(),
                "near_edge_reclaim_close_leaks_past_t3": reclaim_close_leaks,
                "near_edge_reclaim_phase3_flag": phase3_reclaim,
                "absorption_confirmed_at": None if pd.isna(abs_at) else abs_at.isoformat(),
                "absorption_feature_end_phase3": None
                if pd.isna(abs_feature_end_phase3)
                else abs_feature_end_phase3.isoformat(),
                "absorption_leaks_past_t3": bool(
                    pd.notna(abs_feature_end_phase3) and pd.notna(t3) and abs_feature_end_phase3 > t3
                ),
                "defended_at": None if pd.isna(_ts(ep.get("defend_at"))) else str(ep.get("defend_at")),
                "swept_at": None if pd.isna(_ts(ep.get("sweep_at"))) else str(ep.get("sweep_at")),
                "reclaimed_at": None if pd.isna(_ts(ep.get("reclaim_at"))) else str(ep.get("reclaim_at")),
                "accepted_at": None,  # not separately stored in Phase-3; derived for CONSUMED
                "outcome_resolved_at": None if pd.isna(resolved_at) else resolved_at.isoformat(),
                "outcome_resolved_source": resolved_src,
                "label_horizon_end": None if pd.isna(label_horizon_end) else label_horizon_end.isoformat(),
                "decision_before_outcome": bool(
                    pd.notna(t3) and pd.notna(resolved_at) and t3 < resolved_at
                ),
                "outcome_timing_class": timing,
                "usable_for_t3_prediction": usable_predict,
                "near_edge_reclaim_in_t3_window": reclaim_flag,
                "definitional_overlap_reclaim_vs_defended": definitional_overlap,
                "near_edge_reclaim_role": (
                    "early_confirmation_or_state_detection_WITH_CLOSE_LEAKAGE"
                    if reclaim_flag
                    else "absent_in_t3"
                ),
            }
        )
    return pd.DataFrame(rows)


def future_only_labels(
    episodes: pd.DataFrame,
    decision_df: pd.DataFrame,
    candles_by_sym_tf: dict[tuple[str, str], pd.DataFrame],
) -> pd.DataFrame:
    """Path labels strictly after decision_at (T3)."""
    rows = []
    dec = decision_df.set_index("episode_id")
    for _, ep in episodes.iterrows():
        eid = ep["episode_id"]
        if eid not in dec.index or not bool(ep.get("analyzable_core")):
            continue
        drow = dec.loc[eid]
        t3 = _ts(drow["decision_at"])
        if pd.isna(t3):
            continue
        if drow["outcome_timing_class"] != "outcome_after_t3":
            # still compute future path for audit, but flag excluded for prediction
            pass
        side = str(ep["side"])
        lower, upper = float(ep["lower_price"]), float(ep["upper_price"])
        near = upper if side == "BID" else lower
        mid = (lower + upper) / 2.0
        key = (ep["symbol"], str(ep["timeframe"]))
        # prefer TF candles; fallback 1m
        bars = candles_by_sym_tf.get(key)
        if bars is None or bars.empty:
            bars = candles_by_sym_tf.get((ep["symbol"], "1m"), pd.DataFrame())
        if bars.empty:
            continue
        # ATR at T3 from prior 14 bars
        hist = bars[bars["open_time"] <= t3].tail(20)
        atr = float((hist["high"] - hist["low"]).tail(14).mean()) if len(hist) >= 5 else np.nan
        path = bars[bars["open_time"] > t3].copy()  # strict after decision
        # also need 1m path for fine ATR moves — load from 1m always for path
        path1 = candles_by_sym_tf.get((ep["symbol"], "1m"), pd.DataFrame())
        path1 = path1[path1["open_time"] > t3] if not path1.empty else path1

        def _hit_away(mult: float, horizon_min: int) -> bool:
            if path1.empty or not atr or atr != atr:
                return False
            end = t3 + pd.Timedelta(minutes=horizon_min)
            sl = path1[path1["open_time"] <= end]
            if sl.empty:
                return False
            if side == "BID":
                return bool((sl["high"] >= near + mult * atr).any())
            return bool((sl["low"] <= near - mult * atr).any())

        def _hit_adverse(mult: float, horizon_min: int) -> bool:
            if path1.empty or not atr or atr != atr:
                return False
            end = t3 + pd.Timedelta(minutes=horizon_min)
            sl = path1[path1["open_time"] <= end]
            if sl.empty:
                return False
            if side == "BID":
                return bool((sl["low"] <= near - mult * atr).any())
            return bool((sl["high"] >= near + mult * atr).any())

        def _first_time_away(mult: float) -> pd.Timestamp:
            if path1.empty or not atr or atr != atr:
                return pd.NaT
            for _, r in path1.iterrows():
                if side == "BID" and float(r["high"]) >= near + mult * atr:
                    return pd.Timestamp(r["open_time"])
                if side == "ASK" and float(r["low"]) <= near - mult * atr:
                    return pd.Timestamp(r["open_time"])
            return pd.NaT

        def _first_time_adverse(mult: float) -> pd.Timestamp:
            if path1.empty or not atr or atr != atr:
                return pd.NaT
            for _, r in path1.iterrows():
                if side == "BID" and float(r["low"]) <= near - mult * atr:
                    return pd.Timestamp(r["open_time"])
                if side == "ASK" and float(r["high"]) >= near + mult * atr:
                    return pd.Timestamp(r["open_time"])
            return pd.NaT

        # hold reclaim further N minutes: if near edge reclaimed by T3, check closes stay on good side
        held = {}
        for hm in (1, 3, 5):
            end = t3 + pd.Timedelta(minutes=hm)
            sl = path1[(path1["open_time"] > t3) & (path1["open_time"] <= end)] if not path1.empty else path1
            if sl.empty or not bool(drow.get("near_edge_reclaim_in_t3_window")):
                held[hm] = None
            else:
                if side == "BID":
                    held[hm] = bool((sl["close"] > near).all())
                else:
                    held[hm] = bool((sl["close"] < near).all())

        # edge lost again
        edge_lost = None
        if bool(drow.get("near_edge_reclaim_in_t3_window")) and not path1.empty:
            if side == "BID":
                edge_lost = bool((path1["close"] <= near).any())
            else:
                edge_lost = bool((path1["close"] >= near).any())

        # resweep far edge
        resweep = None
        if not path1.empty:
            if side == "BID":
                resweep = bool((path1["low"] <= lower).any())
            else:
                resweep = bool((path1["high"] >= upper).any())

        # return to mid
        mid_hit = None
        if not path1.empty:
            mid_hit = bool(((path1["low"] <= mid) & (path1["high"] >= mid)).any())

        t_fav_05 = _first_time_away(0.5)
        t_adv_025 = _first_time_adverse(0.25)
        t_fav_10 = _first_time_away(1.0)
        t_adv_05 = _first_time_adverse(0.5)

        rows.append(
            {
                "episode_id": eid,
                "symbol": ep["symbol"],
                "side": side,
                "timeframe": ep["timeframe"],
                "decision_at": t3.isoformat(),
                "atr_at_t3": atr,
                "usable_for_t3_prediction": bool(drow["usable_for_t3_prediction"]),
                "hold_reclaim_1m": held[1],
                "hold_reclaim_3m": held[3],
                "hold_reclaim_5m": held[5],
                "edge_lost_again": edge_lost,
                "move_away_0_25atr_5m": _hit_away(0.25, 5),
                "move_away_0_5atr_15m": _hit_away(0.5, 15),
                "move_away_1_0atr_30m": _hit_away(1.0, 30),
                "adverse_0_25atr_5m": _hit_adverse(0.25, 5),
                "adverse_0_5atr_15m": _hit_adverse(0.5, 15),
                "return_to_mid": mid_hit,
                "resweep_far_edge": resweep,
                "next_pool_reached": None,  # no next-pool graph in Phase-3 artifacts
                "next_pool_reached_note": "NOT_COMPUTED_NO_POOL_GRAPH",
                "fav0_5_before_adv0_25": bool(
                    pd.notna(t_fav_05)
                    and (pd.isna(t_adv_025) or t_fav_05 < t_adv_025)
                ),
                "fav1_0_before_adv0_5": bool(
                    pd.notna(t_fav_10) and (pd.isna(t_adv_05) or t_fav_10 < t_adv_05)
                ),
                # post-T3 consumed/accepted proxy: resweep (far-edge re-entry)
                "consumed_accepted_after_t3": bool(resweep),
                "path_starts_strictly_after_decision_at": True,
            }
        )
    return pd.DataFrame(rows)


def eval_future_oos(
    decision_df: pd.DataFrame,
    future_df: pd.DataFrame,
    feat_df: pd.DataFrame,
    splits: dict,
) -> pd.DataFrame:
    """Evaluate Phase-3 rules against future-only labels; splits frozen from feat_df."""
    m = decision_df.merge(future_df, on="episode_id", how="inner", suffixes=("", "_f"))
    m = m.merge(
        feat_df[["episode_id", "temporal_split", "near_edge_reclaim", "absorption_flag"]],
        on="episode_id",
        how="left",
    )
    # only prediction-usable
    m = m[m["usable_for_t3_prediction"] == True]  # noqa: E712

    rules = [
        ("A_near_edge_reclaim", lambda d: d["near_edge_reclaim"].fillna(False).astype(bool)),
        ("B_absorption", lambda d: d["absorption_flag"].fillna(False).astype(bool)),
        (
            "C_reclaim_and_absorption",
            lambda d: d["near_edge_reclaim"].fillna(False).astype(bool)
            & d["absorption_flag"].fillna(False).astype(bool),
        ),
    ]
    labels = [
        "hold_reclaim_5m",
        "move_away_0_5atr_15m",
        "fav0_5_before_adv0_25",
        "fav1_0_before_adv0_5",
        "edge_lost_again",
        "resweep_far_edge",
        "adverse_0_5atr_15m",
    ]
    rows = []
    for rid, mask_fn in rules:
        for lab in labels:
            if lab not in m.columns:
                continue
            for split in ("discovery", "validation", "oos"):
                sub = m[m["temporal_split"] == split]
                y_all = sub[lab].fillna(False).astype(bool)
                sel = sub[mask_fn(sub)]
                n = len(sel)
                coverage = (n / len(sub)) if len(sub) else None
                if n == 0 or sel[lab].isna().all():
                    rows.append(
                        {
                            "rule_id": rid,
                            "future_label": lab,
                            "split": split,
                            "n": n,
                            "coverage": coverage,
                            "precision": None,
                            "recall": None,
                            "baseline": None,
                            "lift": None,
                            "wilson_lo": None,
                            "wilson_hi": None,
                            "coverage_n_split": len(sub),
                            "stratum": "all",
                        }
                    )
                    continue
                y = sel[lab].fillna(False).astype(bool)
                s = int(y.sum())
                prec = s / n
                pos = int(y_all.sum())
                recall = (s / pos) if pos else None
                base = float(y_all.mean()) if len(sub) else None
                lo, hi = wilson_interval(s, n)
                rows.append(
                    {
                        "rule_id": rid,
                        "future_label": lab,
                        "split": split,
                        "n": n,
                        "coverage": coverage,
                        "precision": prec,
                        "recall": recall,
                        "baseline": base,
                        "lift": (prec - base) if base is not None else None,
                        "wilson_lo": lo,
                        "wilson_hi": hi,
                        "coverage_n_split": len(sub),
                        "stratum": "all",
                    }
                )
                # OOS strata (no threshold tuning)
                if split == "oos" and n > 0:
                    for col in ("symbol", "side", "timeframe"):
                        if col not in sel.columns:
                            continue
                        for key, g in sel.groupby(col):
                            yg = g[lab].fillna(False).astype(bool)
                            ng = len(g)
                            sg = int(yg.sum())
                            rows.append(
                                {
                                    "rule_id": rid,
                                    "future_label": lab,
                                    "split": "oos",
                                    "n": ng,
                                    "coverage": None,
                                    "precision": sg / ng if ng else None,
                                    "recall": None,
                                    "baseline": None,
                                    "lift": None,
                                    "wilson_lo": None,
                                    "wilson_hi": None,
                                    "coverage_n_split": len(sub),
                                    "stratum": f"{col}={key}",
                                }
                            )
            # OOS bootstrap for key labels
            oos = m[m["temporal_split"] == "oos"]
            sel = oos[mask_fn(oos)].copy()
            if len(sel) and lab in ("hold_reclaim_5m", "fav0_5_before_adv0_25", "move_away_0_5atr_15m"):
                sel["_hit"] = sel[lab].fillna(False).astype(bool)
                sel["utc_day"] = pd.to_datetime(sel["decision_at"]).dt.strftime("%Y-%m-%d")
                br = block_bootstrap_rate(sel, success_col="_hit", block_cols=["utc_day"], n_boot=200)
                rows.append(
                    {
                        "rule_id": rid,
                        "future_label": lab,
                        "split": "oos_bootstrap",
                        "n": br["n"],
                        "coverage": (len(sel) / len(oos)) if len(oos) else None,
                        "precision": br["rate"],
                        "recall": None,
                        "baseline": None,
                        "lift": None,
                        "wilson_lo": br["boot_lo"],
                        "wilson_hi": br["boot_hi"],
                        "coverage_n_split": len(oos),
                        "stratum": "all",
                    }
                )
    return pd.DataFrame(rows)


def selection_bias(episodes: pd.DataFrame, v2_ent: pd.DataFrame | None = None) -> pd.DataFrame:
    rows = []
    ep = episodes.copy()
    ep["group"] = np.where(ep["analyzable_core"] == True, "analyzable", "not_analyzable")  # noqa: E712
    ep["ft"] = pd.to_datetime(ep["first_touch_at"], errors="coerce")
    ep["width"] = ep["upper_price"] - ep["lower_price"]
    for g, sub in ep.groupby("group"):
        row = {
            "group": g,
            "n": len(sub),
            "defense_rate": float((sub["label_primary"] == "DEFENDED").mean()),
            "sweep_reclaim_rate": float((sub["label_primary"] == "SWEPT_RECLAIMED").mean()),
            "consume_rate": float((sub["label_primary"] == "CONSUMED_ACCEPTED").mean()),
            "unresolved_rate": float((sub["label_primary"] == "unresolved").mean()),
            "swept_rate": float(sub["swept"].fillna(False).astype(bool).mean())
            if "swept" in sub.columns
            else None,
            "mean_n_components": float(sub["n_components"].mean()),
            "mean_distance_atr": float(pd.to_numeric(sub["distance_from_price_atr"], errors="coerce").mean()),
            "mean_cluster_width": float(sub["width"].mean()),
            "mean_mfe_frac": float(pd.to_numeric(sub.get("mfe_frac"), errors="coerce").mean())
            if "mfe_frac" in sub.columns
            else None,
            "mean_mae_frac": float(pd.to_numeric(sub.get("mae_frac"), errors="coerce").mean())
            if "mae_frac" in sub.columns
            else None,
            "first_touch_min": None if sub["ft"].isna().all() else sub["ft"].min().isoformat(),
            "first_touch_max": None if sub["ft"].isna().all() else sub["ft"].max().isoformat(),
            "first_touch_median": None if sub["ft"].isna().all() else sub["ft"].median().isoformat(),
        }
        for col in ("symbol", "timeframe", "side"):
            vc = sub[col].value_counts(normalize=True)
            for k, v in vc.items():
                row[f"share_{col}_{k}"] = float(v)
        rows.append(row)
    return pd.DataFrame(rows)


def _gap_count(gaps: Any) -> int:
    if gaps is None or gaps is False:
        return 0
    if isinstance(gaps, list):
        return len(gaps)
    try:
        return int(gaps)
    except Exception:  # noqa: BLE001
        return 0


def inventory_raw_ob(archive_roots: list[Path]) -> tuple[pd.DataFrame, dict[str, Any]]:
    inv_rows = []
    summary: dict[str, Any] = {"roots": [], "phase3_loader": {}}
    for root in archive_roots:
        root = Path(root)
        info = {
            "path": str(root),
            "exists": root.exists(),
            "n_closed_segments": 0,
            "n_tmp_excluded": 0,
            "n_genuine_replayable_with_snapshot": 0,
            "symbols": [],
            "min_start": None,
            "max_end": None,
        }
        if root.exists():
            segs = list_closed_segments(
                root, symbols=("BTCUSDT", "DOGEUSDT", "XRPUSDT"), include_boundary_stubs=False
            )
            tmps = excluded_tmp_files(root, ("BTCUSDT", "DOGEUSDT", "XRPUSDT"))
            info["n_closed_segments"] = len(segs)
            info["n_tmp_excluded"] = len(tmps)
            info["symbols"] = sorted({s.symbol for s in segs})
            if segs:
                info["min_start"] = min(s.start_utc for s in segs).isoformat()
                info["max_end"] = max(s.end_utc for s in segs).isoformat()
            genuine = 0
            for s in segs:
                man = {}
                mp = s.manifest_path
                if mp.is_file():
                    try:
                        man = json.loads(mp.read_text(encoding="utf-8"))
                    except Exception:  # noqa: BLE001
                        man = {"read_error": True}
                snap = int(man.get("native_snapshot_count") or 0)
                status = str(man.get("completion_status") or "")
                replayable = bool(man.get("replayable"))
                gap_n = _gap_count(man.get("sequence_gaps"))
                u_gap_n = _gap_count(man.get("u_gaps"))
                is_genuine = (
                    status == "closed"
                    and replayable
                    and snap >= 1
                    and gap_n == 0
                    and u_gap_n == 0
                )
                if is_genuine:
                    genuine += 1
                inv_rows.append(
                    {
                        "archive_root": str(root),
                        "symbol": s.symbol,
                        "segment_path": str(s.path),
                        "start_utc": s.start_utc.isoformat(),
                        "end_utc": s.end_utc.isoformat(),
                        "duration_sec": s.duration_sec,
                        "completion_status": man.get("completion_status"),
                        "replayable": man.get("replayable"),
                        "depth": man.get("depth"),
                        "native_snapshot_count": snap,
                        "checkpoint_count": man.get("checkpoint_count"),
                        "delta_count": man.get("delta_count"),
                        "sequence_gap_count": gap_n,
                        "u_gap_count": u_gap_n,
                        "queue_overflow": man.get("queue_overflow"),
                        "format_version": man.get("format_version"),
                        "parser_version": man.get("parser_version"),
                        "genuine_replayable": is_genuine,
                        "is_tmp": False,
                    }
                )
            info["n_genuine_replayable_with_snapshot"] = genuine
        summary["roots"].append(info)

    # Phase-3 loader probe documentation
    summary["phase3_loader"] = {
        "used_path": "ClickHouse orderbook_analysis.orderbook_deltas (SELECT 1 probe)",
        "code": "liquidity_location_r6_orderflow_confirmation_v1/coverage.py:probe_raw_ob200_available",
        "aggregate_used": "orderbook_analysis.orderbook_features_1s_v2",
        "filesystem_archive_checked_in_phase3": False,
        "filesystem_archives_found_now": [r["path"] for r in summary["roots"] if r["exists"]],
        "explanation": (
            "Phase-3 blocker 'orderbook_deltas broken' refers to the ClickHouse MergeTree "
            "table failing ASYNC_LOAD (TOO_MANY_UNEXPECTED_DATA_PARTS). It does NOT prove "
            "absence of filesystem raw OB200 segments under data/orderbook_raw_shadow/ob200_v3. "
            "Phase-3 never called list_closed_segments / RawArchiveManager. "
            "Shadow segments currently have completion_status=open and replayable=false "
            "(0 genuine closed+replayable+snapshot segments for R6)."
        ),
    }
    return pd.DataFrame(inv_rows), summary


def coverage_by_episode(
    episodes: pd.DataFrame,
    archive_root: Path,
    *,
    pre_touch_min: int = 5,
    t3_sec: int = PRIMARY_T3_SEC,
) -> pd.DataFrame:
    segs = list_closed_segments(
        archive_root, symbols=("BTCUSDT", "DOGEUSDT", "XRPUSDT"), include_boundary_stubs=False
    )
    by_sym: dict[str, list] = {}
    for s in segs:
        by_sym.setdefault(s.symbol, []).append(s)

    rows = []
    for _, ep in episodes.iterrows():
        t2 = _ts(ep.get("first_touch_at"))
        if pd.isna(t2):
            rows.append(
                {
                    "episode_id": ep["episode_id"],
                    "symbol": ep["symbol"],
                    "analyzable_raw_ob": False,
                    "reject_reason": "missing_first_touch",
                }
            )
            continue
        need_a = t2 - pd.Timedelta(minutes=pre_touch_min)
        need_b = t2 + pd.Timedelta(seconds=t3_sec)
        need_a_u = need_a.tz_localize("UTC") if need_a.tzinfo is None else need_a
        need_b_u = need_b.tz_localize("UTC") if need_b.tzinfo is None else need_b
        sym = ep["symbol"]
        cand = by_sym.get(sym, [])
        covering = [s for s in cand if s.start_utc < need_b_u and s.end_utc > need_a_u]
        reason = None
        analyzable = False
        snap_ok = False
        gap_n = 0
        u_gap_n = 0
        replayable = None
        depth = None
        status = None
        max_levels = None
        if sym not in by_sym or not by_sym[sym]:
            reason = "no_closed_segments_for_symbol"
        elif not covering:
            reason = "no_segment_overlap_with_need_window"
        else:
            any_genuine = False
            for s in covering:
                mp = s.manifest_path
                if not mp.is_file():
                    continue
                man = json.loads(mp.read_text(encoding="utf-8"))
                snap_n = int(man.get("native_snapshot_count") or 0)
                snap_ok = snap_ok or snap_n >= 1
                gap_n = max(gap_n, _gap_count(man.get("sequence_gaps")))
                u_gap_n = max(u_gap_n, _gap_count(man.get("u_gaps")))
                replayable = man.get("replayable")
                depth = man.get("depth")
                status = man.get("completion_status")
                max_levels = depth
                if (
                    str(status) == "closed"
                    and bool(replayable)
                    and snap_n >= 1
                    and _gap_count(man.get("sequence_gaps")) == 0
                    and _gap_count(man.get("u_gaps")) == 0
                ):
                    any_genuine = True
            if any_genuine:
                analyzable = True
                reason = None
            elif gap_n > 0 or u_gap_n > 0:
                reason = "sequence_gaps_or_u_gaps"
            elif not snap_ok:
                reason = "missing_initial_snapshot"
            elif status != "closed":
                reason = f"segment_completion_status={status}"
            elif not replayable:
                reason = "segment_not_replayable"
            else:
                reason = "not_genuine_raw_chain"

        rows.append(
            {
                "episode_id": ep["episode_id"],
                "symbol": sym,
                "timeframe": ep["timeframe"],
                "side": ep["side"],
                "v2_or_phase3_split": ep.get("v2_temporal_split") or ep.get("temporal_split"),
                "need_start": need_a.isoformat(),
                "need_end": need_b.isoformat(),
                "n_covering_segments": len(covering),
                "native_snapshot_ok": snap_ok,
                "sequence_gap_count": gap_n,
                "u_gap_count": u_gap_n,
                "replayable": replayable,
                "completion_status": status,
                "depth": depth,
                "max_levels_available": max_levels,
                "analyzable_raw_ob": analyzable,
                "reject_reason": reason,
            }
        )
    return pd.DataFrame(rows)


def run_audit(
    *,
    phase3_dir: Path = Path(PHASE3_DIR_DEFAULT),
    out_dir: Path = Path(OUT_DIR_DEFAULT),
) -> dict[str, Any]:
    t0 = time.time()
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    phase3_dir = Path(phase3_dir)

    episodes = pd.read_csv(phase3_dir / "r6_episodes.csv", low_memory=False)
    feat = pd.read_csv(phase3_dir / "feature_matrix_t3.csv", low_memory=False)
    cov = pd.read_csv(phase3_dir / "coverage_by_episode.csv", low_memory=False)
    splits = json.loads((phase3_dir / "temporal_splits.json").read_text())
    p3_manifest = json.loads((phase3_dir / "manifest.json").read_text())

    # attach analyzable
    if "analyzable_core" not in episodes.columns:
        episodes = episodes.merge(cov[["episode_id", "analyzable_core", "status"]], on="episode_id", how="left")

    print("LOAD_CANDLES", flush=True)
    client = get_clickhouse_client()
    tmin = pd.to_datetime(episodes["known_at"]).min() - pd.Timedelta(days=1)
    tmax = pd.to_datetime(episodes["first_touch_at"]).max() + pd.Timedelta(days=1)
    candles_1m: dict[str, pd.DataFrame] = {}
    candles_tf: dict[tuple[str, str], pd.DataFrame] = {}
    for sym in sorted(episodes["symbol"].unique()):
        df1 = fetch_candles_1m(client, sym, tmin.to_pydatetime(), tmax.to_pydatetime())
        candles_1m[sym] = df1
        candles_tf[(sym, "1m")] = df1
        for tf in sorted(episodes.loc[episodes.symbol == sym, "timeframe"].unique()):
            candles_tf[(sym, str(tf))] = aggregate_timeframe(df1, str(tf))

    print("DECISION_TIMESTAMPS", flush=True)
    decision_df = build_decision_timestamps(episodes, feat, candles_1m)
    # attach phase3 temporal split from feat
    decision_df = decision_df.merge(
        feat[["episode_id", "temporal_split"]].rename(columns={"temporal_split": "temporal_split_phase3"}),
        on="episode_id",
        how="left",
    )
    if "temporal_split_phase3_x" in decision_df.columns:
        decision_df["temporal_split_phase3"] = decision_df["temporal_split_phase3_y"].combine_first(
            decision_df["temporal_split_phase3_x"]
        )
        decision_df = decision_df.drop(columns=["temporal_split_phase3_x", "temporal_split_phase3_y"])

    analyzable = decision_df[decision_df["analyzable_core"] == True]  # noqa: E712
    timing_counts = analyzable["outcome_timing_class"].value_counts().to_dict()
    print("timing", timing_counts, flush=True)

    # Leakage verdicts
    n_before = int((analyzable["outcome_timing_class"] == "outcome_before_t3").sum())
    n_abs_leak = int(analyzable["absorption_leaks_past_t3"].fillna(False).sum())
    n_overlap = int(analyzable["definitional_overlap_reclaim_vs_defended"].fillna(False).sum())
    n_reclaim_close_leak = int(
        analyzable["near_edge_reclaim_close_leaks_past_t3"].fillna(False).sum()
    )
    reclaim_is_state = True  # by construction feature is within [T2,T3)

    print("FUTURE_LABELS", flush=True)
    future_df = future_only_labels(episodes, decision_df, candles_tf)

    print("FUTURE_OOS", flush=True)
    # restrict feat near_edge to bool
    rule_oos = eval_future_oos(decision_df, future_df, feat, splits)

    bias = selection_bias(episodes)

    print("RAW_INVENTORY", flush=True)
    roots = [Path(SHADOW_ARCHIVE), Path(LIVE_ARCHIVE_DEFAULT)]
    inv_df, inv_summary = inventory_raw_ob(roots)
    shadow = Path(SHADOW_ARCHIVE)
    cov_raw = coverage_by_episode(episodes, shadow) if shadow.exists() else pd.DataFrame()

    # write loader trace
    loader_trace = f"""# Raw OB200 loader trace (Phase-3 vs filesystem)

## What Phase-3 actually probed
1. `probe_raw_ob200_available()` in
   `src/orderbook_analyse/liquidity_location_r6_orderflow_confirmation_v1/coverage.py`
2. Executed: `SELECT 1 FROM orderbook_analysis.orderbook_deltas LIMIT 1`
3. On failure → `per_level_raw_available=False`, reason `orderbook_deltas_unavailable:DatabaseError`
4. Feature path used aggregate only:
   `orderbook_analysis.orderbook_features_1s_v2` via `fetch_ob_agg_1s`

## What `orderbook_deltas broken` means
- ClickHouse table `orderbook_analysis.orderbook_deltas` fails to attach/load
  (`TOO_MANY_UNEXPECTED_DATA_PARTS` / `ASYNC_LOAD_WAIT_FAILED`).
- This is a **ClickHouse storage/attach** failure for that table.
- It is **not** a statement that no raw archive files exist on disk.

## Filesystem archives found in this audit
{json.dumps(inv_summary, indent=2, default=str)}

## Correct raw loader path for Phase-4 (not used in Phase-3)
1. `OB_V3_RAW_ARCHIVE_ROOT` or default `data/orderbook_raw_live/ob200_v3`
2. Shadow copy present: `data/orderbook_raw_shadow/ob200_v3`
3. Discover closed segments: `ob200_v3_raw_discovery.files.list_closed_segments`
   (excludes `*_open_*.zst.tmp`)
4. Replay: `orderbook_v2_live.raw_archive.replay.replay_segment`
5. Require `native_snapshot_count >= 1` before delta chain

## Gap
Phase-3 never imported `list_closed_segments` / never scanned the archive root.
Therefore the Phase-3 report understated filesystem raw availability.
"""
    (out_dir / "raw_ob_loader_trace.md").write_text(loader_trace, encoding="utf-8")

    _write_csv(out_dir / "decision_label_timestamps.csv", decision_df)
    _write_csv(out_dir / "future_only_labels.csv", future_df)
    _write_csv(out_dir / "rule_future_only_oos.csv", rule_oos)
    _write_csv(out_dir / "analyzable_selection_bias.csv", bias)
    _write_csv(out_dir / "raw_ob_inventory.csv", inv_df)
    _write_csv(out_dir / "raw_ob_coverage_by_episode.csv", cov_raw)

    # Phase-4 recommendation
    n_raw_ok = int(cov_raw["analyzable_raw_ob"].sum()) if len(cov_raw) else 0
    n_raw_oos = 0
    n_overlap_segments = 0
    if len(cov_raw):
        tmp = cov_raw.merge(feat[["episode_id", "temporal_split"]], on="episode_id", how="left")
        n_raw_oos = int(((tmp["analyzable_raw_ob"]) & (tmp["temporal_split"] == "oos")).sum())
        n_overlap_segments = int((pd.to_numeric(cov_raw.get("n_covering_segments"), errors="coerce").fillna(0) > 0).sum())

    n_genuine_segs = 0
    for rinfo in inv_summary.get("roots", []):
        n_genuine_segs += int(rinfo.get("n_genuine_replayable_with_snapshot") or 0)
    shadow_exists = Path(SHADOW_ARCHIVE).exists() and any(
        r.get("n_closed_segments", 0) > 0 for r in inv_summary.get("roots", [])
    )

    # D = Phase-3 never wired FS loader (routing bug) while files exist.
    # B/C = readiness for Phase-4 given genuine replayable coverage.
    if n_raw_ok >= 40 and n_raw_oos >= 10:
        phase4_rec = "A. RAW_OB200_READY_FOR_R6_PHASE4"
    elif shadow_exists and n_genuine_segs == 0 and n_overlap_segments > 0:
        # Files overlap some R6 windows but are not genuine/replayable → limited smoke only
        phase4_rec = "B. RAW_OB200_PARTIAL_COVERAGE"
    elif shadow_exists and n_raw_ok == 0:
        phase4_rec = "B. RAW_OB200_PARTIAL_COVERAGE"
    elif not shadow_exists:
        phase4_rec = "C. RAW_OB200_NOT_AVAILABLE"
    elif n_raw_ok > 0:
        phase4_rec = "B. RAW_OB200_PARTIAL_COVERAGE"
    else:
        phase4_rec = "C. RAW_OB200_NOT_AVAILABLE"

    # Secondary diagnosis: Phase-3 probed CH only → loader path incomplete
    loader_path_issue = shadow_exists and not inv_summary["phase3_loader"].get(
        "filesystem_archive_checked_in_phase3", True
    )

    leakage_found = bool(
        n_abs_leak > 0 or reclaim_is_state or n_before > 0 or n_reclaim_close_leak > 0
    )
    if leakage_found:
        verdict = VERDICT_LEAKAGE
    elif loader_path_issue and n_raw_ok == 0 and n_genuine_segs > 0:
        verdict = VERDICT_LOADER_BROKEN
    else:
        verdict = VERDICT_COMPLETE

    methodology = f"""# Phase-3 Audit Methodology

## Near-edge reclaim
{json.dumps(NEAR_EDGE_RECLAIM_DEF, indent=2)}

## DEFENDED
{json.dumps(DEFENDED_DEF, indent=2)}

## SWEPT_RECLAIMED
{json.dumps(SWEPT_RECLAIMED_DEF, indent=2)}

## Absorption
{json.dumps(ABSORPTION_DEF, indent=2)}

## Overlap conclusion
`near_edge_reclaim` is measured on 1m closes for bars with open_time in `[T2, T3)`.
It is **not identical** to DEFENDED (no full sweep + 0.5 ATR away, often later).
It is an **in-window state / early confirmation**, not a pure post-T3 DEFENDED predictor.
Additionally, for T3=30s the close of the included bar is typically only known at T2+1m (>T3)
→ **close leakage**. Rename: `early_confirmation` / `state_detection`. Future-only labels start at T3.

## Absorption leakage
Phase-3 `touch_price_continuation` candle slice ends at `T2+5s+1m`, which is **after**
`decision_at=T2+30s`. Therefore absorption_flag can use post-T3 price information.

## Future-only labels
All path metrics use bars with `open_time > decision_at`.

## Splits
Unchanged Phase-3 temporal splits from `temporal_splits.json`.
"""
    (out_dir / "methodology.md").write_text(methodology, encoding="utf-8")

    manifest = {
        "audit_id": AUDIT_ID,
        "verdict": verdict,
        "phase4_recommendation": phase4_rec,
        "phase3_dir": str(phase3_dir),
        "primary_t3_sec": PRIMARY_T3_SEC,
        "n_analyzable": int(analyzable.shape[0]),
        "outcome_timing_counts": timing_counts,
        "n_outcome_before_t3": n_before,
        "n_absorption_leaks_past_t3": n_abs_leak,
        "n_definitional_overlap_reclaim_defended": n_overlap,
        "n_near_edge_reclaim_close_leaks_past_t3": n_reclaim_close_leak,
        "near_edge_reclaim_reclassified_as": "early_confirmation_or_state_detection",
        "n_raw_ob_analyzable_episodes": n_raw_ok,
        "n_raw_ob_analyzable_oos": n_raw_oos,
        "n_episodes_with_segment_overlap": n_overlap_segments,
        "n_genuine_segments_archive": n_genuine_segs,
        "phase3_loader_path_incomplete": loader_path_issue,
        "raw_inventory_summary": inv_summary,
        "phase3_manifest_verdict": p3_manifest.get("verdict"),
        "splits_frozen_from_phase3": splits,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "elapsed_sec": round(time.time() - t0, 2),
        "no_commit": True,
        "no_phase4_run": True,
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")

    write_report(out_dir, manifest, decision_df, rule_oos, bias, cov_raw, timing_counts)
    print("VERDICT", verdict, flush=True)
    print("PHASE4_REC", phase4_rec, flush=True)
    return {"verdict": verdict, "manifest": manifest, "out_dir": str(out_dir)}


def write_report(
    out_dir: Path,
    manifest: dict,
    decision_df: pd.DataFrame,
    rule_oos: pd.DataFrame,
    bias: pd.DataFrame,
    cov_raw: pd.DataFrame,
    timing_counts: dict,
) -> None:
    v = manifest["verdict"]
    ana = decision_df[decision_df["analyzable_core"] == True]  # noqa: E712
    lines = [
        f"# {v}",
        "",
        "## 1. VERDICT",
        v,
        f"Phase-4 recommendation: **{manifest['phase4_recommendation']}**",
        "",
        "## 2. LIVE-SICHERHEIT",
        "- No commit, no collector touch, no CH writes, no locks, no Phase-4 feature run, no bot/PnL.",
        "",
        "## 3. PHASE-3-REGELPARITÄT",
        f"- Phase-3 verdict was: {manifest.get('phase3_manifest_verdict')}",
        "- Rules audited: R6_near_edge_reclaim, R6_absorption (and combination).",
        "- T3 remains 30s after first_touch (frozen).",
        "",
        "## 4. DECISION-/LABEL-TRENNUNG",
        f"- Analyzable timing counts: {timing_counts}",
        f"- outcome_before_t3: {manifest['n_outcome_before_t3']}",
        f"- usable_for_t3_prediction (outcome_after_t3): "
        f"{int((ana['outcome_timing_class']=='outcome_after_t3').sum())}",
        "- Requirement `decision_at < outcome_resolved_at` enforced for prediction-usable set.",
        "",
        "## 5. NEAR-EDGE-RECLAIM-ÜBERLAPPUNG",
        "- Definition: 1m close beyond near edge for bars with open_time in [T2, T3).",
        "- DEFENDED: no full sweep + 0.5 ATR away reaction (may resolve after T3).",
        "- Not identical to DEFENDED, but **in-window state/early_confirmation**, not a pure future predictor.",
        f"- Close-known-at > T3 (close leakage): {manifest.get('n_near_edge_reclaim_close_leaks_past_t3')}",
        f"- Reclassified as: `{manifest['near_edge_reclaim_reclassified_as']}`",
        f"- Overlap rows (DEFENDED & Phase-3 reclaim flag): {manifest['n_definitional_overlap_reclaim_defended']}",
        "",
        "## 6. ABSORPTION-LEAKAGE",
        f"- Episodes where Phase-3 absorption candle window ends after T3: {manifest['n_absorption_leaks_past_t3']}",
        "- Cause: `extract_trade_features` uses candles `[touch_start, touch_end+1m)` for impact;",
        "  touch_end=T2+5s ⇒ feature_end=T2+65s > decision_at=T2+30s.",
        "- Trades themselves end at T2+5s (OK); price continuation leaks.",
        "",
        "## 7. FUTURE-ONLY-OOS",
        "See `rule_future_only_oos.csv` (splits frozen from Phase-3).",
    ]
    if len(rule_oos):
        oos = rule_oos[rule_oos["split"] == "oos"]
        if len(oos):
            lines.append(oos.head(30).to_string(index=False))
    lines += [
        "",
        "## 8. COVERAGE-SELEKTIONSBIAS",
        bias.to_string(index=False) if len(bias) else "n/a",
        "",
        "## 9. RAW-OB-INVENTAR",
        json.dumps(manifest.get("raw_inventory_summary"), indent=2, default=str),
        "",
        "## 10. LOADERPFAD",
        "See `raw_ob_loader_trace.md`. Phase-3 probed CH `orderbook_deltas` only;",
        "filesystem `data/orderbook_raw_shadow/ob200_v3` was not scanned.",
        "",
        "## 11. RAW-COVERAGE JE EPISODE",
        f"- analyzable_raw_ob episodes: {manifest['n_raw_ob_analyzable_episodes']}",
        f"- of which Phase-3 OOS split: {manifest['n_raw_ob_analyzable_oos']}",
    ]
    if len(cov_raw):
        lines.append(cov_raw["reject_reason"].fillna("ok").value_counts().to_string())
        lines.append(cov_raw.groupby("symbol")["analyzable_raw_ob"].mean().to_string())
    lines += [
        "",
        "## 12. BLOCKER",
        "- Label leakage / state-detection misuse for near-edge reclaim vs DEFENDED.",
        "- Absorption post-T3 candle leakage in Phase-3 feature code.",
        "- Raw OB FS archive starts ~2026-08-24 22:47 UTC (BTC/DOGE only; no XRP);",
        "  most R6 episodes (from 2026-08-06) have no raw segment overlap.",
        "- CH `orderbook_deltas` remains broken (unrelated to FS archive existence).",
        "",
        "## 13. EMPFEHLUNG FÜR PHASE 4",
        manifest["phase4_recommendation"],
        "1) Fix feature causality (absorption candle end ≤ T3) before any new rule claims.",
        "2) Treat near-edge reclaim as early_confirmation; evaluate future-only path labels.",
        "3) Wire Phase-4 loader to `list_closed_segments` on archive root (not CH deltas).",
        "4) With current archive span, only a short recent R6 subset is raw-eligible → partial smoke.",
        f"",
        f"Elapsed: {manifest.get('elapsed_sec')}s",
    ]
    (out_dir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

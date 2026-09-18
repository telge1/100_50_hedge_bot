"""Orchestrate big-move case-control comparison (read-only inputs)."""

from __future__ import annotations

import csv
import json
import math
import os
import resource
import time
import traceback
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from obfull_research_engine.mp_big_move_case_control_v1 import PACKAGE_NAME, RUN_ID
from obfull_research_engine.mp_big_move_case_control_v1.chart_select import (
    build_chart_pairs,
    select_chart_cases,
)
from obfull_research_engine.mp_big_move_case_control_v1.context_features import compute_price_context
from obfull_research_engine.mp_big_move_case_control_v1.matching import build_matched_pairs
from obfull_research_engine.mp_big_move_case_control_v1.outcome_classes import (
    classify_outcome,
    write_outcome_contract,
)
from obfull_research_engine.mp_big_move_case_control_v1.params import (
    BATCH_RUN_REL,
    CONFIRM_RUN_REL,
    DEFAULT_RUN_REL,
    ENRICH_RUN_REL,
    MANUAL_TRIGGERS_UTC,
    NS,
    OB_FEATURE_CANDIDATES,
    PRICE_PATH_RUN_REL,
    WALL_FILTER_REL,
)
from obfull_research_engine.mp_big_move_case_control_v1.stats import (
    compare_feature,
    median,
    quantile,
    spearman,
)
from obfull_research_engine.mp_edge_event_study_v1.util import ns_to_dt
from obfull_research_engine.mp_price_path_4h_v1.candles import load_candles_1m
from obfull_research_engine.mp_price_path_4h_v1.wall_filter import (
    event_passes_wall_persistence,
    load_frozen_wall_filter,
)


def _peak_rss_mib() -> float:
    return float(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss) / 1024.0


def _write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    keys: list[str] = []
    seen: set[str] = set()
    for r in rows:
        for k in r:
            if k not in seen and "bps" not in str(k).lower().replace("spread_mean_bps", "spread_mean_pct_proxy"):
                # allow spread_mean_bps column name but note it's stored as-is from features (raw); user asked percent outputs for excursions
                if k not in seen:
                    seen.add(k)
                    keys.append(k)
    # simplify: keep all except nested
    keys = []
    seen = set()
    for r in rows:
        for k, v in r.items():
            if isinstance(v, (list, dict)):
                continue
            if k not in seen:
                seen.add(k)
                keys.append(k)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=keys, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k) for k in keys})
    os.replace(tmp, path)


def _to_parquet(path: Path, rows: list[dict[str, Any]]) -> None:
    import pandas as pd

    path.parent.mkdir(parents=True, exist_ok=True)
    clean = []
    for r in rows:
        clean.append({k: v for k, v in r.items() if not isinstance(v, (list, dict))})
    pd.DataFrame(clean).to_parquet(path, index=False)


def _fmt(ns: int | None) -> str | None:
    if ns is None:
        return None
    return ns_to_dt(int(ns)).strftime("%Y-%m-%d %H:%M:%S UTC")


def _parse_utc(s: str) -> int:
    t = s.replace("Z", "")
    dt = datetime.fromisoformat(t).replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * NS)


def _truthy(v: Any) -> bool:
    return str(v).strip().lower() in ("true", "1", "yes")


def _path_rows_by_event(df) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list] = defaultdict(list)
    for r in df.to_dict(orient="records"):
        out[str(r["event_id"])].append(r)
    for eid in out:
        out[eid] = sorted(out[eid], key=lambda x: int(x.get("minutes_since_trigger", x.get("minutes_since_entry", 0))))
    return out


def run_analysis(*, repo_root: Path) -> dict[str, Any]:
    repo_root = Path(repo_root)
    out_dir = repo_root / DEFAULT_RUN_REL
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "analysis.log"
    t0 = time.time()

    def log(msg: str) -> None:
        line = f"{datetime.now(timezone.utc).isoformat()} {msg}"
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        print(line, flush=True)

    log(f"START {PACKAGE_NAME} {RUN_ID}")
    contract = write_outcome_contract(out_dir / "frozen_outcome_class_contract.json")

    import pandas as pd

    batch = repo_root / BATCH_RUN_REL
    enrich = repo_root / ENRICH_RUN_REL
    price = repo_root / PRICE_PATH_RUN_REL
    conf = repo_root / CONFIRM_RUN_REL

    events = pd.read_parquet(batch / "events_all.parquet")
    feats = pd.read_parquet(enrich / "event_features.parquet")
    sel = pd.read_parquet(batch / "selection_policies.parquet")
    hz = pd.read_parquet(price / "event_horizon_excursions.parquet")
    paths = pd.read_parquet(price / "event_price_paths.parquet")
    split = json.loads((enrich / "discovery_validation_split.json").read_text())
    ovc = pd.read_parquet(conf / "original_vs_confirmed.parquet")
    conf_entries = pd.read_parquet(conf / "confirmed_entries.parquet")
    conf_paths = pd.read_parquet(conf / "confirmed_price_paths.parquet")
    wall_frozen = load_frozen_wall_filter(repo_root / WALL_FILTER_REL)

    feat_map = {str(r["event_id"]): r for r in feats.to_dict(orient="records")}
    wall_flags = {
        eid: event_passes_wall_persistence(fr, wall_frozen) for eid, fr in feat_map.items()
    }
    ft_ids = set(sel[sel["policy"] == "FIRST_TOUCH_PER_ZONE_PROFILE_VERSION"]["event_id"].astype(str))
    h240 = hz[(hz["horizon_min"] == 240) & (hz["is_complete"] == True)].copy()  # noqa: E712
    h240_map = {str(r["event_id"]): r for r in h240.to_dict(orient="records")}
    path_map = _path_rows_by_event(paths)
    conf_path_map = _path_rows_by_event(conf_paths)
    conf_map = {str(r["event_id"]): r for r in conf_entries.to_dict(orient="records")}
    ovc_map = {str(r["event_id"]): r for r in ovc.to_dict(orient="records")}
    disc_windows = set(split["discovery_window_ids"])
    val_windows = set(split["validation_window_ids"])

    available_ob = [c for c in OB_FEATURE_CANDIDATES if c in feats.columns]

    input_audit = {
        "ok": True,
        "n_events": len(events),
        "first_touch_policy_ids": len(ft_ids),
        "complete_4h": len(h240_map),
        "available_ob_features": available_ob,
        "missing_requested_ob": [c for c in OB_FEATURE_CANDIDATES if c not in feats.columns],
        "note": "wall_present_fraction mapped to baseline/approach/contact fractions; imbalance/spread/flow_flip use existing names",
        "discovery_windows": list(disc_windows),
        "validation_windows": list(val_windows),
        "no_mp_recompute": True,
        "no_ob_recompute": True,
        "no_confirm_recompute": True,
    }

    # primary universe: first-touch ∩ tradable ∩ complete 4h
    primary_events = []
    for r in events.to_dict(orient="records"):
        eid = str(r["event_id"])
        if eid not in ft_ids:
            continue
        if eid not in h240_map:
            continue
        if r.get("label_price_only") not in ("ABSORB", "FAILED_BREAK", "TRUE_BREAK"):
            continue
        if r.get("trade_side") not in ("LONG", "SHORT"):
            continue
        if _truthy(r.get("is_censored", False)):
            continue
        if eid not in path_map:
            continue
        primary_events.append(r)
    input_audit["first_touch_tradable_complete"] = len(primary_events)
    input_audit["ok"] = len(primary_events) >= 50
    _write_json(out_dir / "input_audit.json", input_audit)
    if not input_audit["ok"]:
        return {"verdict": "BIG_MOVE_COMPARISON_BLOCKED_INPUT", "input_audit": input_audit}

    # load candles once for context
    from obfull_research_engine.clickhouse_research_store_v1.helpers import get_clickhouse_client

    client = get_clickhouse_client(role="mp_big_move_case_control_read")
    tmin = min(int(e["trigger_ts_ns"]) for e in primary_events)
    tmax = max(int(e["trigger_ts_ns"]) for e in primary_events)
    # also cover confirmed entries
    for eid, ce in conf_map.items():
        if ce.get("entry_ts_ns") is not None:
            tmax = max(tmax, int(ce["entry_ts_ns"]))
            tmin = min(tmin, int(ce.get("alert_ts_ns") or ce["entry_ts_ns"]))
    start = ns_to_dt(tmin - 10 * 3600 * NS)
    end = ns_to_dt(tmax + 1 * 3600 * NS)
    log(f"LOAD_CANDLES {start} -> {end}")
    candles = load_candles_1m(client, start=start, end=end)
    log(f"CANDLES={len(candles)}")

    def build_row(event: dict[str, Any], *, entry_mode: str) -> dict[str, Any] | None:
        eid = str(event["event_id"])
        side = str(event["trade_side"])
        if entry_mode == "ORIGINAL":
            ref_ts = int(event["trigger_ts_ns"])
            ref_px = float(event["trigger_price"])
            prows = path_map.get(eid)
            h = h240_map.get(eid)
            if not prows or not h:
                return None
            mfe = float(h["mfe_pct"])
            mae = float(h["mae_pct"])
            close_r = float(h["close_return_pct"]) if h.get("close_return_pct") is not None else None
        else:
            ce = conf_map.get(eid)
            if not ce or not ce.get("confirmed"):
                return None
            ref_ts = int(ce["entry_ts_ns"])
            ref_px = float(ce["entry_price"])
            prows = conf_path_map.get(eid)
            if not prows:
                return None
            # last running values
            last = prows[-1]
            mfe = float(last["running_mfe_pct"])
            mae = float(last["running_mae_pct"])
            close_r = float(last["signed_close_return_pct"]) if last.get("signed_close_return_pct") is not None else None

        cls = classify_outcome(prows, mfe_4h=mfe, mae_4h=mae, close_4h=close_r)
        ctx = compute_price_context(candles, reference_entry_ts_ns=ref_ts, trade_side=side)
        fr = feat_map.get(eid, {})
        row: dict[str, Any] = {
            "event_id": eid,
            "entry_mode": entry_mode,
            "reference_entry_ts_ns": ref_ts,
            "reference_entry_ts_utc": _fmt(ref_ts),
            "reference_entry_price": ref_px,
            "label_price_only": str(event["label_price_only"]),
            "trade_side": side,
            "event_role": str(event.get("event_role")),
            "confluence_class": str(event.get("confluence_class")),
            "window_id": str(event.get("window_id")),
            "split": "DISCOVERY" if str(event.get("window_id")) in disc_windows else (
                "VALIDATION" if str(event.get("window_id")) in val_windows else "OTHER"
            ),
            "first_touch_policy": eid in ft_ids,
            "wall_persistence": bool(wall_flags.get(eid, False)),
            "alert_ts_ns": int(event["trigger_ts_ns"]),
            "alert_ts_utc": _fmt(int(event["trigger_ts_ns"])),
            "trigger_price": float(event["trigger_price"]),
            **cls,
            **{k: ctx.get(k) for k in ctx},
        }
        # confirmation meta
        ce = conf_map.get(eid)
        o = ovc_map.get(eid)
        if ce:
            row["confirmed_entry_ts_ns"] = ce.get("entry_ts_ns")
            row["confirmed_entry_ts_utc"] = ce.get("entry_ts_utc")
            row["confirmation_delay_minutes"] = ce.get("alert_to_entry_minutes")
            row["confirmation_reset_count"] = ce.get("confirmation_reset_count") or ce.get("sequence_reset_count")
            row["confirmed_primary_result"] = ce.get("primary_result")
        if o:
            row["original_primary_result"] = o.get("original_primary_result")
            row["comparison_class"] = o.get("comparison_class")
            row["confirmed_flag"] = o.get("confirmed")

        for col in available_ob:
            raw = fr.get(col)
            avail = fr.get(f"{col}__available")
            if avail is False or raw is None or raw == "" or raw == "None" or (isinstance(raw, float) and math.isnan(raw)):
                row[col] = None
                row[f"{col}__status"] = "NOT_AVAILABLE"
            else:
                try:
                    row[col] = float(raw)
                    row[f"{col}__status"] = "OK"
                except (TypeError, ValueError):
                    row[col] = None
                    row[f"{col}__status"] = "NOT_AVAILABLE"
        # chart window
        chart_start = ns_to_dt(int(event["trigger_ts_ns"])) - timedelta(minutes=30)
        chart_end = ns_to_dt(ref_ts) + timedelta(hours=4)
        row["chart_start_utc"] = chart_start.strftime("%Y-%m-%d %H:%M:%S UTC")
        row["chart_end_utc"] = chart_end.strftime("%Y-%m-%d %H:%M:%S UTC")
        return row

    original_rows = []
    for e in primary_events:
        r = build_row(e, entry_mode="ORIGINAL")
        if r and r.get("leakage_check_passed", True):
            original_rows.append(r)
    log(f"ORIGINAL_ROWS={len(original_rows)}")

    # secondary confirmed for same primary event ids
    confirmed_rows = []
    for e in primary_events:
        r = build_row(e, entry_mode="CONFIRMED")
        if r is not None:
            confirmed_rows.append(r)
    log(f"CONFIRMED_ROWS={len(confirmed_rows)}")

    all_class_rows = original_rows + confirmed_rows
    _to_parquet(out_dir / "event_outcome_classes.parquet", all_class_rows)
    _to_parquet(out_dir / "causal_context_features.parquet", all_class_rows)

    # discovery quantile thresholds from ORIGINAL discovery split
    disc = [r for r in original_rows if r.get("split") == "DISCOVERY"]
    val = [r for r in original_rows if r.get("split") == "VALIDATION"]
    mfe_vals = [float(r["mfe_4h_pct"]) for r in disc if r.get("mfe_4h_pct") is not None]
    mae_b_vals = [float(r["mae_before_0_41_pct"]) for r in disc if r.get("mae_before_0_41_pct") is not None]
    thr = {
        "mfe_q25": quantile(mfe_vals, 0.25),
        "mfe_q75": quantile(mfe_vals, 0.75),
        "mae_before_041_q25": quantile(mae_b_vals, 0.25),
        "mae_before_041_q75": quantile(mae_b_vals, 0.75),
        "n_discovery": len(disc),
        "frozen": True,
        "source_split": "enrichment discovery windows",
    }
    _write_json(out_dir / "discovery_quantile_thresholds.json", thr)

    def flag_quartiles(rows: list[dict[str, Any]]) -> None:
        for r in rows:
            mfe = r.get("mfe_4h_pct")
            mae_b = r.get("mae_before_0_41_pct")
            r["top_mfe_quartile"] = mfe is not None and thr["mfe_q75"] is not None and float(mfe) >= thr["mfe_q75"]
            r["bottom_mfe_quartile"] = mfe is not None and thr["mfe_q25"] is not None and float(mfe) <= thr["mfe_q25"]
            r["low_mae_before_quartile"] = (
                mae_b is not None and thr["mae_before_041_q25"] is not None and float(mae_b) <= thr["mae_before_041_q25"]
            )
            r["high_mae_before_quartile"] = (
                mae_b is not None and thr["mae_before_041_q75"] is not None and float(mae_b) >= thr["mae_before_041_q75"]
            )

    flag_quartiles(original_rows)
    flag_quartiles(confirmed_rows)

    # feature lists for comparison
    numeric_features = list(available_ob) + [
        "return_previous_15m_pct",
        "return_previous_30m_pct",
        "return_previous_60m_pct",
        "return_previous_240m_pct",
        "range_previous_30m_pct",
        "range_previous_60m_pct",
        "atr14_5m_pct",
        "vol_current_5m_pct",
        "pos_in_prior_1h_range",
        "dist_ema9_pct",
        "dist_ema20_pct",
        "dist_ema59_pct",
        "dist_ema200_pct",
        "slope_ema9",
        "slope_ema20",
        "slope_ema59",
        "slope_ema200",
        "higher_highs_30m",
        "lower_highs_30m",
        "higher_lows_30m",
        "lower_lows_30m",
        "confirmation_delay_minutes",
    ]

    def group_vals(rows: list[dict[str, Any]], feat: str, pred) -> list[Any]:
        return [r.get(feat) for r in rows if pred(r)]

    comparisons = []

    def add_cmp(rows, feat, name_a, pred_a, name_b, pred_b, split_name="ALL"):
        a = group_vals(rows, feat, pred_a)
        b = group_vals(rows, feat, pred_b)
        row = compare_feature(a, b, feature=feat, group_a=name_a, group_b=name_b)
        # spearman vs mfe / mae_before on same rows universe
        ys_mfe = [r.get("mfe_4h_pct") for r in rows]
        ys_mae = [r.get("mae_before_0_41_pct") for r in rows]
        xs = [r.get(feat) for r in rows]
        row["spearman_mfe_4h"] = spearman(xs, ys_mfe)
        row["spearman_mae_before_041"] = spearman(xs, ys_mae)
        row["split"] = split_name
        comparisons.append(row)

    main_pairs = [
        ("BIG_CLEAN_MOVE", lambda r: r["primary_outcome_class"] in ("BIG_CLEAN_MOVE", "VERY_BIG_CLEAN_MOVE"),
         "NO_EXPANSION", lambda r: r["primary_outcome_class"] == "NO_EXPANSION"),
        ("BIG_CLEAN_MOVE", lambda r: r["primary_outcome_class"] in ("BIG_CLEAN_MOVE", "VERY_BIG_CLEAN_MOVE"),
         "WRONG_WAY", lambda r: r["primary_outcome_class"] == "WRONG_WAY"),
        ("BIG_CLEAN_MOVE", lambda r: r["primary_outcome_class"] in ("BIG_CLEAN_MOVE", "VERY_BIG_CLEAN_MOVE"),
         "BIG_DIRTY_MOVE", lambda r: r["primary_outcome_class"] == "BIG_DIRTY_MOVE"),
        ("QUALIFIED_MOVE", lambda r: bool(r.get("is_qualified_move")),
         "STOP_FIRST", lambda r: r.get("target_stop_order") in ("STOP_FIRST", "STOP_THEN_TARGET", "AMBIGUOUS")),
        ("TOP_MFE_Q", lambda r: bool(r.get("top_mfe_quartile")),
         "BOTTOM_MFE_Q", lambda r: bool(r.get("bottom_mfe_quartile"))),
    ]

    for split_name, subset in (("ALL", original_rows), ("DISCOVERY", disc), ("VALIDATION", val)):
        for feat in numeric_features:
            for na, pa, nb, pb in main_pairs:
                add_cmp(subset, feat, na, pa, nb, pb, split_name=split_name)
            # wall within label
            for lab in ("ABSORB", "FAILED_BREAK", "TRUE_BREAK"):
                add_cmp(
                    subset,
                    feat,
                    f"WALL_TRUE_{lab}",
                    lambda r, L=lab: r.get("label_price_only") == L and r.get("wall_persistence"),
                    f"WALL_FALSE_{lab}",
                    lambda r, L=lab: r.get("label_price_only") == L and not r.get("wall_persistence"),
                    split_name=split_name,
                )
            # trend with vs against
            add_cmp(
                subset,
                feat,
                "WITH_MED_TREND",
                lambda r: r.get("trade_with_medium_term_trend") is True,
                "AGAINST_MED_TREND",
                lambda r: r.get("trade_with_medium_term_trend") is False,
                split_name=split_name,
            )

    # discovery vs validation sign agreement on BIG_CLEAN vs WRONG_WAY
    def sign(x):
        if x is None:
            return 0
        if x > 1e-12:
            return 1
        if x < -1e-12:
            return -1
        return 0

    disc_cmp = [c for c in comparisons if c["split"] == "DISCOVERY" and c["group_a"] == "BIG_CLEAN_MOVE" and c["group_b"] == "WRONG_WAY"]
    val_cmp = {
        (c["feature"], c["group_a"], c["group_b"]): c
        for c in comparisons
        if c["split"] == "VALIDATION" and c["group_a"] == "BIG_CLEAN_MOVE" and c["group_b"] == "WRONG_WAY"
    }
    for c in disc_cmp:
        v = val_cmp.get((c["feature"], c["group_a"], c["group_b"]))
        c["discovery_sign"] = sign(c.get("cliffs_delta"))
        c["validation_sign"] = sign(v.get("cliffs_delta")) if v else None
        c["same_direction_in_validation"] = (
            c["discovery_sign"] != 0 and c["validation_sign"] == c["discovery_sign"]
        ) if v else False

    # also attach to ALL rows for same comparison
    for c in comparisons:
        if c["split"] != "ALL":
            continue
        if c["group_a"] == "BIG_CLEAN_MOVE" and c["group_b"] == "WRONG_WAY":
            d = next((x for x in disc_cmp if x["feature"] == c["feature"]), None)
            if d:
                c["discovery_sign"] = d.get("discovery_sign")
                c["validation_sign"] = d.get("validation_sign")
                c["same_direction_in_validation"] = d.get("same_direction_in_validation")

    _write_csv(out_dir / "feature_comparison_all.csv", [c for c in comparisons if c["split"] == "ALL"])
    _write_csv(out_dir / "feature_comparison_discovery.csv", [c for c in comparisons if c["split"] == "DISCOVERY"])
    _write_csv(out_dir / "feature_comparison_validation.csv", [c for c in comparisons if c["split"] == "VALIDATION"])

    # matched pairs
    pairs = build_matched_pairs(original_rows, min_score=65)
    _write_csv(out_dir / "matched_case_pairs.csv", pairs)

    # summaries by label / side
    def class_counts(rows):
        d = defaultdict(int)
        for r in rows:
            d[r["primary_outcome_class"]] += 1
        return dict(d)

    by_label = []
    for lab in ("ABSORB", "FAILED_BREAK", "TRUE_BREAK"):
        sub = [r for r in original_rows if r["label_price_only"] == lab]
        by_label.append({"label": lab, "n": len(sub), **{f"n_{k}": v for k, v in class_counts(sub).items()}})
    _write_csv(out_dir / "comparison_by_label.csv", by_label)
    by_side = []
    for side in ("LONG", "SHORT"):
        sub = [r for r in original_rows if r["trade_side"] == side]
        by_side.append({"trade_side": side, "n": len(sub), **{f"n_{k}": v for k, v in class_counts(sub).items()}})
    _write_csv(out_dir / "comparison_by_trade_side.csv", by_side)

    # original vs confirmed by label
    ovc_by_lab = []
    for lab in ("ABSORB", "FAILED_BREAK", "TRUE_BREAK"):
        sub = ovc[ovc["label_price_only"] == lab]
        # restrict to first-touch primary set
        sub = sub[sub["event_id"].astype(str).isin({str(e["event_id"]) for e in primary_events})]
        n = len(sub)
        orig_q = int((sub["original_primary_result"] == "TARGET_FIRST").sum())
        conf_sub = sub[sub["confirmed"] == True]  # noqa: E712
        conf_q = int((conf_sub["confirmed_primary_result"] == "TARGET_FIRST").sum()) if len(conf_sub) else 0
        ovc_by_lab.append(
            {
                "label": lab,
                "n_first_touch": n,
                "original_qualified_rate": orig_q / n if n else None,
                "n_confirmed": len(conf_sub),
                "confirmed_qualified_rate": conf_q / len(conf_sub) if len(conf_sub) else None,
                "avoided_bad_trades": int((sub["comparison_class"] == "AVOIDED_BAD_TRADE").sum()),
                "improved": int((sub["comparison_class"] == "IMPROVED").sum()),
                "degraded": int((sub["comparison_class"] == "DEGRADED").sum()),
                "missed_winners": int((sub["comparison_class"] == "MISSED_WINNER").sum()),
                "preserved": int((sub["comparison_class"] == "PRESERVED").sum()),
            }
        )
    _write_csv(out_dir / "original_vs_confirmed_by_label.csv", ovc_by_lab)

    # chart cases
    chart_rows = []
    for cls_name in (
        "BIG_CLEAN_MOVE",
        "VERY_BIG_CLEAN_MOVE",
        "NO_EXPANSION",
        "WRONG_WAY",
        "BIG_DIRTY_MOVE",
        "STOP_THEN_LATE_TARGET",
    ):
        selected = select_chart_cases(original_rows, class_name=cls_name, n=3)
        for r in selected:
            chart_rows.append(
                {
                    "selection_class": cls_name,
                    "event_id": r["event_id"],
                    "primary_outcome_class": r["primary_outcome_class"],
                    "label": r["label_price_only"],
                    "trade_side": r["trade_side"],
                    "event_role": r["event_role"],
                    "confluence_class": r["confluence_class"],
                    "wall_persistence": r["wall_persistence"],
                    "trigger_ts": r["alert_ts_utc"],
                    "trigger_price": r["trigger_price"],
                    "confirmed_entry_ts": r.get("confirmed_entry_ts_utc"),
                    "mfe_4h_pct": r["mfe_4h_pct"],
                    "mae_4h_pct": r["mae_4h_pct"],
                    "mae_before_0_41_pct": r["mae_before_0_41_pct"],
                    "minutes_to_0_41_pct": r["minutes_to_0_41_pct"],
                    "close_return_4h_pct": r["close_return_4h_pct"],
                    "hit_qty": r.get("hit_qty"),
                    "refill_ratio": r.get("refill_ratio"),
                    "wall_survival_after_hits": r.get("wall_survival_after_hits"),
                    "aggression_without_progress": r.get("aggression_without_progress"),
                    "imbalance_at_trigger": r.get("imbalance_at_trigger"),
                    "trade_with_medium_term_trend": r.get("trade_with_medium_term_trend"),
                    "trade_against_ema200": r.get("trade_against_ema200"),
                    "dist_ema59_pct": r.get("dist_ema59_pct"),
                    "atr14_5m_pct": r.get("atr14_5m_pct"),
                    "chart_start_utc": r["chart_start_utc"],
                    "chart_end_utc": r["chart_end_utc"],
                }
            )
    _write_csv(out_dir / "selected_chart_cases.csv", chart_rows)

    clean_sel = [r for r in original_rows if r["event_id"] in {c["event_id"] for c in chart_rows if c["selection_class"] in ("BIG_CLEAN_MOVE", "VERY_BIG_CLEAN_MOVE")}]
    ctrl_sel = [r for r in original_rows if r["event_id"] in {c["event_id"] for c in chart_rows if c["selection_class"] in ("NO_EXPANSION", "WRONG_WAY", "BIG_DIRTY_MOVE")}]
    # rebuild from ids
    by_id = {r["event_id"]: r for r in original_rows}
    clean_sel = [by_id[c["event_id"]] for c in chart_rows if c["selection_class"] in ("BIG_CLEAN_MOVE", "VERY_BIG_CLEAN_MOVE")]
    ctrl_sel = [by_id[c["event_id"]] for c in chart_rows if c["selection_class"] in ("NO_EXPANSION", "WRONG_WAY")]
    chart_pairs = build_chart_pairs(clean_sel, ctrl_sel)
    _write_csv(out_dir / "selected_chart_pairs.csv", chart_pairs)

    # three existing cases — match by label+side+nearest trigger (not first-touch-only nearest)
    manual_specs = [
        {"label": "FAILED_BREAK", "side": "LONG", "ts": _parse_utc("2026-09-10T16:44:17")},
        {"label": "ABSORB", "side": "LONG", "ts": _parse_utc("2026-09-06T21:48:26")},
        {"label": "TRUE_BREAK", "side": "SHORT", "ts": _parse_utc("2026-09-11T07:33:36")},
    ]
    all_events = [
        r
        for r in events.to_dict(orient="records")
        if r.get("label_price_only") in ("ABSORB", "FAILED_BREAK", "TRUE_BREAK")
        and r.get("trade_side") in ("LONG", "SHORT")
        and str(r["event_id"]) in h240_map
        and str(r["event_id"]) in path_map
    ]
    # build ORIGINAL rows for any missing manual events outside first-touch primary set
    extra_original = {r["event_id"]: r for r in original_rows}
    for e in all_events:
        eid = str(e["event_id"])
        if eid not in extra_original:
            rr = build_row(e, entry_mode="ORIGINAL")
            if rr:
                extra_original[eid] = rr
    extra_confirmed = {r["event_id"]: r for r in confirmed_rows}
    for e in all_events:
        eid = str(e["event_id"])
        if eid not in extra_confirmed:
            rr = build_row(e, entry_mode="CONFIRMED")
            if rr:
                extra_confirmed[eid] = rr

    three_lines = [
        "# Three Existing Cases — Original vs Confirmed Classification",
        "",
        "Rules were not changed based on these cases.",
        "",
    ]
    three_summary = []
    for i, spec in enumerate(manual_specs, 1):
        cands = [
            e
            for e in all_events
            if e.get("label_price_only") == spec["label"] and e.get("trade_side") == spec["side"]
        ]
        best = min(cands, key=lambda e: abs(int(e["trigger_ts_ns"]) - spec["ts"]))
        eid = str(best["event_id"])
        orig = extra_original.get(eid)
        conf_r = extra_confirmed.get(eid)
        three_lines += [
            f"## CASE {i} — {best.get('label_price_only')} {best.get('trade_side')}",
            "",
            f"- alert: {_fmt(int(best['trigger_ts_ns']))}",
            f"- event_id: `{eid}`",
            f"- first_touch_policy: {eid in ft_ids}",
            f"- ORIGINAL class: {orig.get('primary_outcome_class') if orig else None}",
            f"- ORIGINAL order: {orig.get('target_stop_order') if orig else None}",
            f"- ORIGINAL MFE/MAE/MAE_before: {orig.get('mfe_4h_pct') if orig else None} / {orig.get('mae_4h_pct') if orig else None} / {orig.get('mae_before_0_41_pct') if orig else None}",
            f"- ORIGINAL trend with medium: {orig.get('trade_with_medium_term_trend') if orig else None}, against EMA200: {orig.get('trade_against_ema200') if orig else None}",
            f"- ORIGINAL OB: hit_qty={orig.get('hit_qty') if orig else None}, refill={orig.get('refill_ratio') if orig else None}, wall_surv={orig.get('wall_survival_after_hits') if orig else None}, aggr_wo_prog={orig.get('aggression_without_progress') if orig else None}",
            f"- CONFIRMED present: {conf_r is not None}",
            f"- CONFIRMED class: {conf_r.get('primary_outcome_class') if conf_r else None}",
            f"- CONFIRMED order: {conf_r.get('target_stop_order') if conf_r else None}",
            f"- CONFIRMED MFE/MAE/MAE_before: {conf_r.get('mfe_4h_pct') if conf_r else None} / {conf_r.get('mae_4h_pct') if conf_r else None} / {conf_r.get('mae_before_0_41_pct') if conf_r else None}",
            f"- comparison_class: {(ovc_map.get(eid) or {}).get('comparison_class')}",
            "",
        ]
        three_summary.append(
            {
                "case": i,
                "event_id": eid,
                "alert_ts_utc": _fmt(int(best["trigger_ts_ns"])),
                "original_class": orig.get("primary_outcome_class") if orig else None,
                "confirmed_class": conf_r.get("primary_outcome_class") if conf_r else None,
                "comparison_class": (ovc_map.get(eid) or {}).get("comparison_class"),
            }
        )
    (out_dir / "THREE_EXISTING_CASES.md").write_text("\n".join(three_lines), encoding="utf-8")

    # top distinguishing features: ALL BIG_CLEAN vs WRONG_WAY by |cliffs|, coverage
    top = [
        c
        for c in comparisons
        if c["split"] == "ALL"
        and c["group_a"] == "BIG_CLEAN_MOVE"
        and c["group_b"] == "WRONG_WAY"
        and c.get("cliffs_delta") is not None
        and c.get("n_a", 0) >= 5
        and c.get("n_b", 0) >= 5
    ]
    top = sorted(top, key=lambda c: abs(float(c["cliffs_delta"])), reverse=True)[:10]
    same_dir = [
        c
        for c in disc_cmp
        if c.get("same_direction_in_validation")
        and c.get("n_a", 0) >= 3
        and abs(float(c.get("cliffs_delta") or 0)) >= 0.1
    ]
    same_dir = sorted(same_dir, key=lambda c: abs(float(c["cliffs_delta"])), reverse=True)[:15]

    # wall effect
    wall_true_q = [r for r in original_rows if r.get("wall_persistence") and r.get("is_qualified_move")]
    wall_false_q = [r for r in original_rows if (not r.get("wall_persistence")) and r.get("is_qualified_move")]
    wall_true_n = sum(1 for r in original_rows if r.get("wall_persistence"))
    wall_false_n = sum(1 for r in original_rows if not r.get("wall_persistence"))
    wall_effect = {
        "wall_true_n": wall_true_n,
        "wall_true_qualified_rate": len(wall_true_q) / wall_true_n if wall_true_n else None,
        "wall_false_n": wall_false_n,
        "wall_false_qualified_rate": len(wall_false_q) / wall_false_n if wall_false_n else None,
    }

    with_med = [r for r in original_rows if r.get("trade_with_medium_term_trend") is True]
    against_med = [r for r in original_rows if r.get("trade_with_medium_term_trend") is False]
    trend_effect = {
        "with_med_n": len(with_med),
        "with_med_qualified_rate": sum(1 for r in with_med if r.get("is_qualified_move")) / len(with_med) if with_med else None,
        "against_med_n": len(against_med),
        "against_med_qualified_rate": sum(1 for r in against_med if r.get("is_qualified_move")) / len(against_med) if against_med else None,
    }

    counts = class_counts(original_rows)
    leakage_violations = sum(1 for r in original_rows if not r.get("leakage_check_passed", True))

    runtime = {
        "runtime_seconds": time.time() - t0,
        "peak_rss_mib": _peak_rss_mib(),
        "n_original": len(original_rows),
        "n_confirmed": len(confirmed_rows),
    }
    _write_json(out_dir / "runtime_metrics.json", runtime)

    # verdict
    n_clean = counts.get("BIG_CLEAN_MOVE", 0) + counts.get("VERY_BIG_CLEAN_MOVE", 0)
    if leakage_violations:
        verdict = "BIG_MOVE_COMPARISON_BLOCKED_IMPLEMENTATION"
    elif n_clean < 3 or counts.get("WRONG_WAY", 0) + counts.get("NO_EXPANSION", 0) < 3:
        verdict = "BIG_MOVE_COMPARISON_SUCCESS_LOW_SAMPLE"
    elif not top:
        verdict = "BIG_MOVE_COMPARISON_NO_SEPARATION"
    elif len(original_rows) < 40:
        verdict = "BIG_MOVE_COMPARISON_SUCCESS_LOW_SAMPLE"
    else:
        verdict = "BIG_MOVE_COMPARISON_SUCCESS"

    summary = {
        "verdict": verdict,
        "first_touch_events": len(original_rows),
        "class_counts": counts,
        "top10_features_clean_vs_wrong": [
            {"feature": c["feature"], "cliffs_delta": c["cliffs_delta"], "median_a": c["median_a"], "median_b": c["median_b"]}
            for c in top
        ],
        "same_sign_discovery_validation": [c["feature"] for c in same_dir],
        "wall_effect": wall_effect,
        "trend_effect": trend_effect,
        "original_vs_confirmed_by_label": ovc_by_lab,
        "n_matched_pairs": len(pairs),
        "top_chart_pairs": chart_pairs[:3],
        "three_existing_cases": three_summary,
        "leakage_violations": leakage_violations,
        "runtime": runtime,
        "contract_hash": contract.get("contract_hash_sha256"),
    }

    report = [
        "# Big Move Case-Control Comparison Report",
        "",
        f"## Verdict",
        "",
        f"`{verdict}`",
        "",
        "No new entry rule. No parameter tuning. Descriptive separation only.",
        "",
        "## Summary",
        "",
        "```json",
        json.dumps(summary, indent=2, sort_keys=True, default=str),
        "```",
        "",
        "## Units",
        "",
        "Excursions and returns in **percent**.",
        "",
    ]
    (out_dir / "BIG_MOVE_COMPARISON_REPORT.md").write_text("\n".join(report), encoding="utf-8")
    _write_json(
        out_dir / "run_manifest.json",
        {
            "package": PACKAGE_NAME,
            "run_id": RUN_ID,
            "created_utc": datetime.now(timezone.utc).isoformat(),
            "verdict": verdict,
            "summary": summary,
            "inputs": {
                "batch": str(batch),
                "enrich": str(enrich),
                "price_path": str(price),
                "confirmation": str(conf),
            },
        },
    )
    log(f"DONE {verdict}")
    return {"verdict": verdict, "summary": summary, "out_dir": str(out_dir)}


def main(argv: list[str] | None = None) -> int:
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument("--repo-root", type=Path, default=Path.cwd())
    args = p.parse_args(argv)
    try:
        run_analysis(repo_root=Path(args.repo_root))
        return 0
    except Exception:
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

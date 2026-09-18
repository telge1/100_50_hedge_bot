"""Discovery/validation split and descriptive feature analysis (no ML)."""

from __future__ import annotations

import csv
import math
from collections import defaultdict
from pathlib import Path
from typing import Any

from .params import FEATURE_GROUPS


def _truthy(v: Any) -> bool:
    return str(v).strip().lower() in {"1", "true", "yes", "y"}


def _f(v: Any) -> float | None:
    if v is None or v == "" or v == "None":
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(x) or math.isinf(x):
        return None
    return x


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    keys: list[str] = []
    seen: set[str] = set()
    for r in rows:
        for k in r:
            if k not in seen:
                seen.add(k)
                keys.append(k)
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=keys, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k) for k in keys})


def build_discovery_validation_split(
    *,
    windows_csv: Path,
    events: list[dict[str, str]],
    discovery_window_count: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    with windows_csv.open(encoding="utf-8", newline="") as fh:
        wins = [r for r in csv.DictReader(fh) if str(r.get("included")).lower() == "true"]
    wins = sorted(wins, key=lambda r: int(r["start_ns"]))
    n_disc = int(discovery_window_count)
    if n_disc < 1 or n_disc >= len(wins):
        raise RuntimeError("INVALID_DISCOVERY_WINDOW_COUNT")
    disc_ids = {w["window_id"] for w in wins[:n_disc]}
    val_ids = {w["window_id"] for w in wins[n_disc:]}
    if disc_ids & val_ids:
        raise RuntimeError("SPLIT_WINDOW_OVERLAP")
    hours = []
    for w in wins:
        hours.append((w["window_id"], float(w["duration_s"]) / 3600.0))
    disc_h = sum(h for wid, h in hours if wid in disc_ids)
    val_h = sum(h for wid, h in hours if wid in val_ids)
    rows = []
    for e in events:
        wid = e["window_id"]
        if wid in disc_ids:
            split = "DISCOVERY"
        elif wid in val_ids:
            split = "VALIDATION"
        else:
            raise RuntimeError(f"EVENT_WINDOW_NOT_IN_SPLIT:{e['event_id']}:{wid}")
        rows.append(
            {
                "event_id": e["event_id"],
                "window_id": wid,
                "split": split,
                "first_touch_ts_ns": e.get("first_touch_ts_ns"),
            }
        )
    meta = {
        "discovery_window_ids": sorted(disc_ids),
        "validation_window_ids": sorted(val_ids),
        "discovery_hours": disc_h,
        "validation_hours": val_h,
        "discovery_share": disc_h / (disc_h + val_h) if (disc_h + val_h) else None,
        "n_discovery_events": sum(1 for r in rows if r["split"] == "DISCOVERY"),
        "n_validation_events": sum(1 for r in rows if r["split"] == "VALIDATION"),
        "rule": "first_N_complete_windows_by_start_ts=DISCOVERY; remainder=VALIDATION; fixed pre-outcome",
        "discovery_window_count": n_disc,
    }
    return rows, meta


def _spearman(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 5 or len(xs) != len(ys):
        return None

    def rank(vals: list[float]) -> list[float]:
        order = sorted(range(len(vals)), key=lambda i: vals[i])
        ranks = [0.0] * len(vals)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and vals[order[j + 1]] == vals[order[i]]:
                j += 1
            avg = (i + j) / 2.0 + 1.0
            for k in range(i, j + 1):
                ranks[order[k]] = avg
            i = j + 1
        return ranks

    rx, ry = rank(xs), rank(ys)
    n = len(xs)
    mx = sum(rx) / n
    my = sum(ry) / n
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    denx = math.sqrt(sum((a - mx) ** 2 for a in rx))
    deny = math.sqrt(sum((b - my) ** 2 for b in ry))
    if denx <= 0 or deny <= 0:
        return None
    return num / (denx * deny)


def _quantiles(vals: list[float]) -> dict[str, float | None]:
    if not vals:
        return {"p25": None, "p50": None, "p75": None, "mean": None}
    xs = sorted(vals)

    def q(p: float) -> float:
        pos = p * (len(xs) - 1)
        lo = int(pos)
        hi = min(lo + 1, len(xs) - 1)
        frac = pos - lo
        return xs[lo] * (1 - frac) + xs[hi] * frac

    return {"p25": q(0.25), "p50": q(0.5), "p75": q(0.75), "mean": sum(xs) / len(xs)}


def _cliffs_delta(a: list[float], b: list[float]) -> float | None:
    if not a or not b:
        return None
    gt = sum(1 for x in a for y in b if x > y)
    lt = sum(1 for x in a for y in b if x < y)
    return (gt - lt) / (len(a) * len(b))


def run_analyses(
    *,
    events: list[dict[str, str]],
    episodes: list[dict[str, str]],
    outcomes: list[dict[str, str]],
    feature_rows: list[dict[str, Any]],
    split_rows: list[dict[str, Any]],
    out_dir: Path,
) -> dict[str, Any]:
    ep_by_id = {e["event_id"]: e for e in episodes}
    feat_by_id = {r["event_id"]: r for r in feature_rows}
    split_by_id = {r["event_id"]: r["split"] for r in split_rows}

    # outcomes at 1800s
    out1800 = {
        o["event_id"]: o
        for o in outcomes
        if str(o.get("horizon_s")) == "1800"
    }
    out300 = {o["event_id"]: o for o in outcomes if str(o.get("horizon_s")) == "300"}
    out900 = {o["event_id"]: o for o in outcomes if str(o.get("horizon_s")) == "900"}

    def policy_ids(name: str) -> set[str]:
        if name == "ALL_VALID_EVENTS":
            return {e["event_id"] for e in events}
        if name == "FIRST_TOUCH_PER_ZONE_PROFILE_VERSION":
            return {
                e["event_id"]
                for e in episodes
                if _truthy(e.get("is_first_touch_of_zone_version"))
            }
        if name == "NON_OVERLAPPING_30M_OUTCOMES":
            return {
                e["event_id"]
                for e in episodes
                if _truthy(e.get("selected_non_overlapping_30m"))
            }
        if name == "COOLDOWN_15M":
            return {
                e["event_id"] for e in episodes if _truthy(e.get("selected_cooldown_15m"))
            }
        raise KeyError(name)

    primary_policy = "FIRST_TOUCH_PER_ZONE_PROFILE_VERSION"
    primary_ids = policy_ids(primary_policy)

    # Feature list: numeric feature columns without meta suffixes
    sample = feature_rows[0] if feature_rows else {}
    feature_names = [
        k
        for k in sample.keys()
        if "__" not in k
        and k
        not in {
            "event_id",
            "window_id",
            "replay_epoch",
            "first_touch_ts_ns",
            "trigger_ts_ns",
            "feature_cutoff_ts_ns",
            "max_feature_ts_ns",
            "causal_ok",
            "leakage_flags",
            "trades_available",
            "trades_missing_reason",
            "baseline_warmup_ok",
            "level_changes_available",
            "n_level_changes",
            "n_metrics",
            "n_trades",
            "label_price_only",
            "event_role",
            "trade_side",
            "confluence_class",
        }
    ]

    dist_rows = []
    effect_disc = []
    effect_val = []

    def success_flag(oid: str) -> bool | None:
        o = out1800.get(oid)
        if not o:
            return None
        if _truthy(o.get("is_censored")) or _truthy(o.get("censored")):
            return None
        g = _f(o.get("gross_return_bps"))
        if g is None:
            return None
        return g > 0

    for fname in feature_names:
        for split_name, bucket in (("DISCOVERY", "DISCOVERY"), ("VALIDATION", "VALIDATION"), ("ALL", None)):
            vals_pos: list[float] = []
            vals_neg: list[float] = []
            pairs_x: list[float] = []
            pairs_y: list[float] = []
            for eid in primary_ids:
                if bucket and split_by_id.get(eid) != bucket:
                    continue
                fr = feat_by_id.get(eid)
                if not fr or not fr.get("causal_ok"):
                    continue
                if fr.get(f"{fname}__causal_valid") is False:
                    continue
                if fr.get(f"{fname}__available") is False:
                    continue
                xv = _f(fr.get(fname))
                if xv is None:
                    continue
                sf = success_flag(eid)
                if sf is None:
                    continue
                o = out1800[eid]
                y = _f(o.get("gross_return_bps"))
                if y is None:
                    continue
                pairs_x.append(xv)
                pairs_y.append(y)
                if sf:
                    vals_pos.append(xv)
                else:
                    vals_neg.append(xv)
            qpos = _quantiles(vals_pos)
            qneg = _quantiles(vals_neg)
            row = {
                "feature": fname,
                "split": split_name,
                "policy": primary_policy,
                "n_pos": len(vals_pos),
                "n_neg": len(vals_neg),
                "median_pos": qpos["p50"],
                "median_neg": qneg["p50"],
                "p25_pos": qpos["p25"],
                "p75_pos": qpos["p75"],
                "p25_neg": qneg["p25"],
                "p75_neg": qneg["p75"],
                "cliffs_delta": _cliffs_delta(vals_pos, vals_neg),
                "spearman_gross_1800": _spearman(pairs_x, pairs_y),
            }
            dist_rows.append(row)
            if split_name == "DISCOVERY":
                effect_disc.append(row)
            elif split_name == "VALIDATION":
                effect_val.append(row)

    _write_csv(out_dir / "feature_distributions.csv", dist_rows)
    _write_csv(out_dir / "feature_effects_discovery.csv", effect_disc)
    _write_csv(out_dir / "feature_effects_validation.csv", effect_val)

    # Predeclared group scores on discovery: median split of mean z of available members
    # Derive thresholds from discovery feature medians only.
    group_summary = []
    filter_rows = []

    def net8(oid: str) -> float | None:
        o = out1800.get(oid)
        if not o or _truthy(o.get("is_censored")) or _truthy(o.get("censored")):
            return None
        # Prefer batch net (costs already applied once); never subtract again.
        n = _f(o.get("net_return_bps_8"))
        if n is not None:
            return n
        g = _f(o.get("gross_return_bps"))
        if g is None:
            return None
        return g - 8.0

    def gross_h(oid: str, table: dict) -> float | None:
        o = table.get(oid)
        if not o or _truthy(o.get("is_censored")) or _truthy(o.get("censored")):
            return None
        return _f(o.get("gross_return_bps"))

    def mfe_mae(oid: str) -> tuple[float | None, float | None]:
        o = out1800.get(oid)
        if not o:
            return None, None
        return _f(o.get("mfe_bps_gross") or o.get("mfe_bps")), _f(
            o.get("mae_bps_gross") or o.get("mae_bps")
        )

    # Baseline first-touch discovery/validation
    def mean_net(ids: set[str], split: str | None) -> tuple[float | None, int]:
        vals = []
        for eid in ids:
            if split and split_by_id.get(eid) != split:
                continue
            v = net8(eid)
            if v is not None:
                vals.append(v)
        if not vals:
            return None, 0
        return sum(vals) / len(vals), len(vals)

    base_disc, n_base_disc = mean_net(primary_ids, "DISCOVERY")
    base_val, n_base_val = mean_net(primary_ids, "VALIDATION")
    base_all, n_base_all = mean_net(primary_ids, None)

    best_group = None
    best_score = None

    for gname, members in FEATURE_GROUPS.items():
        # discovery medians per member
        thresholds = {}
        directions = {}  # 1 = high is good if pos median > neg median on discovery
        for m in members:
            disc_row = next((r for r in effect_disc if r["feature"] == m), None)
            if not disc_row or disc_row["median_pos"] is None or disc_row["median_neg"] is None:
                continue
            # threshold = overall discovery median of feature among primary
            vals = []
            for eid in primary_ids:
                if split_by_id.get(eid) != "DISCOVERY":
                    continue
                fr = feat_by_id.get(eid)
                if not fr or not fr.get("causal_ok"):
                    continue
                if fr.get(f"{m}__available") is False:
                    continue
                xv = _f(fr.get(m))
                if xv is not None:
                    vals.append(xv)
            if len(vals) < 8:
                continue
            thr = sorted(vals)[len(vals) // 2]
            thresholds[m] = thr
            directions[m] = 1 if disc_row["median_pos"] >= disc_row["median_neg"] else -1

        def pass_filter(eid: str) -> bool:
            fr = feat_by_id.get(eid)
            if not fr or not fr.get("causal_ok"):
                return False
            ok_any = False
            for m, thr in thresholds.items():
                xv = _f(fr.get(m))
                if xv is None or fr.get(f"{m}__available") is False:
                    continue
                ok_any = True
                if directions[m] > 0 and xv < thr:
                    return False
                if directions[m] < 0 and xv > thr:
                    return False
            return ok_any and bool(thresholds)

        def eval_split(split: str) -> dict[str, Any]:
            ids = [eid for eid in primary_ids if split_by_id.get(eid) == split and pass_filter(eid)]
            nets = [net8(eid) for eid in ids]
            nets = [v for v in nets if v is not None]
            mfes, maes = [], []
            for eid in ids:
                mfe, mae = mfe_mae(eid)
                if mfe is not None:
                    mfes.append(mfe)
                if mae is not None:
                    maes.append(mae)
            return {
                "n": len(nets),
                "mean_net8": sum(nets) / len(nets) if nets else None,
                "median_mfe": sorted(mfes)[len(mfes) // 2] if mfes else None,
                "median_mae": sorted(maes)[len(maes) // 2] if maes else None,
            }

        d = eval_split("DISCOVERY")
        v = eval_split("VALIDATION")
        score = None
        if d["mean_net8"] is not None and base_disc is not None:
            score = d["mean_net8"] - base_disc
        row = {
            "feature_group": gname,
            "n_members_thresholded": len(thresholds),
            "discovery_n": d["n"],
            "discovery_mean_net8": d["mean_net8"],
            "validation_n": v["n"],
            "validation_mean_net8": v["mean_net8"],
            "discovery_lift_vs_base": score,
            "validation_lift_vs_base": (v["mean_net8"] - base_val) if (v["mean_net8"] is not None and base_val is not None) else None,
            "same_direction": (
                score is not None
                and v["mean_net8"] is not None
                and base_val is not None
                and ((score > 0 and (v["mean_net8"] - base_val) > 0) or (score < 0 and (v["mean_net8"] - base_val) < 0))
            ),
            "thresholds": str(thresholds),
        }
        group_summary.append(row)
        filter_rows.append(
            {
                "filter": gname,
                "policy": primary_policy,
                "base_discovery_net8": base_disc,
                "base_validation_net8": base_val,
                "filtered_discovery_net8": d["mean_net8"],
                "filtered_validation_net8": v["mean_net8"],
                "filtered_discovery_n": d["n"],
                "filtered_validation_n": v["n"],
                "discovery_median_mfe": d["median_mfe"],
                "discovery_median_mae": d["median_mae"],
                "validation_median_mfe": v["median_mfe"],
                "validation_median_mae": v["median_mae"],
            }
        )
        if score is not None and d["n"] >= 8:
            if best_score is None or score > best_score:
                best_score = score
                best_group = row

    _write_csv(out_dir / "feature_group_summary.csv", group_summary)
    _write_csv(out_dir / "outcome_filter_comparison.csv", filter_rows)

    # Top descriptive features by |cliffs| on discovery with validation same sign
    ranked = []
    for r in effect_disc:
        if r["cliffs_delta"] is None or r["n_pos"] + r["n_neg"] < 20:
            continue
        vr = next((x for x in effect_val if x["feature"] == r["feature"]), None)
        same = (
            vr is not None
            and vr.get("cliffs_delta") is not None
            and r["cliffs_delta"] * vr["cliffs_delta"] > 0
        )
        ranked.append({**r, "validation_cliffs": None if not vr else vr.get("cliffs_delta"), "same_sign_validation": same})
    ranked.sort(key=lambda x: abs(x["cliffs_delta"] or 0), reverse=True)
    top5 = ranked[:5]

    # C1 vs C2 with best OB filter
    c1_ids = {e["event_id"] for e in events if e.get("confluence_class") == "C1_30M"} & primary_ids
    c2_ids = {
        e["event_id"]
        for e in events
        if str(e.get("confluence_class", "")).startswith("C2")
    } & primary_ids

    def mean_net_set(ids: set[str]) -> tuple[float | None, int]:
        vals = [net8(i) for i in ids]
        vals = [v for v in vals if v is not None]
        return (sum(vals) / len(vals) if vals else None), len(vals)

    c1_base, n_c1 = mean_net_set(c1_ids)
    c2_base, n_c2 = mean_net_set(c2_ids)

    edge_flag = "NO_EDGE"
    if best_group and best_group.get("same_direction") and (best_group.get("discovery_lift_vs_base") or 0) > 0:
        if (best_group.get("discovery_n") or 0) >= 15 and (best_group.get("validation_n") or 0) >= 8:
            edge_flag = "EDGE_CANDIDATE"
        else:
            edge_flag = "LOW_SAMPLE"
    sample_flag = "LOW" if n_base_all < 40 else "OK"

    summary = {
        "base_net8_first_touch_all": base_all,
        "base_net8_discovery": base_disc,
        "base_net8_validation": base_val,
        "n_base_discovery": n_base_disc,
        "n_base_validation": n_base_val,
        "best_feature_group": None if not best_group else best_group.get("feature_group"),
        "best_group_discovery_net8": None if not best_group else best_group.get("discovery_mean_net8"),
        "best_group_validation_net8": None if not best_group else best_group.get("validation_mean_net8"),
        "best_group_discovery_n": None if not best_group else best_group.get("discovery_n"),
        "best_group_validation_n": None if not best_group else best_group.get("validation_n"),
        "top5_features": [
            {
                "feature": r["feature"],
                "cliffs_delta": r["cliffs_delta"],
                "spearman": r["spearman_gross_1800"],
                "same_sign_validation": r["same_sign_validation"],
            }
            for r in top5
        ],
        "c1_30m_net8": c1_base,
        "c1_30m_n": n_c1,
        "c2_confluence_net8": c2_base,
        "c2_confluence_n": n_c2,
        "edge_flag": edge_flag,
        "sample_flag": sample_flag,
    }
    return {"summary": summary, "edge_flag": edge_flag, "sample_flag": sample_flag, "top5": top5, "best_group": best_group}

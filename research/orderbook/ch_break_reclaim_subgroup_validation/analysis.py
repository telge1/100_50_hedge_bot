"""Subgroup early-signal analysis, distance control, transfer, classifications."""

from __future__ import annotations

from typing import Any

from research.orderbook.ch_break_reclaim_subgroup_validation.load import (
    CONFIRMATION_ONLY_TIMEPOINTS,
    EARLY_TIMEPOINTS,
    MIN_N_BREAK,
    MIN_N_OTHER,
    SUBGROUPS,
    event_in_subgroup,
)
from research.orderbook.ch_break_reclaim_subgroup_validation.metrics import (
    average_rank_score,
    best_auc,
    bootstrap_auc_ci,
    cohens_d,
    jackknife_auc,
    mann_whitney_auc,
    median_diff,
    to_float,
)

# Priority single features for early evaluation (dir-aware where noted)
EARLY_FEATURES = (
    "imbalance_0_10",
    "imbalance_0_25",
    "break_side_near_depth",
    "support_near_depth",
    "support_minus_break_depth",
    "support_depth_0_5",
    "break_depth_0_5",
    "support_depth_change_10s",
    "signed_distance_beyond_bps",
    "abs_distance_to_level_bps",
    "distance_to_level_bps",
    "flow_5s_signed_break",
    "flow_10s_signed_break",
    "flow_30s_signed_break",
    "support_frac_0",
)

DISTANCE_FEATURES = (
    "abs_distance_to_level_bps",
    "signed_distance_beyond_bps",
    "distance_to_level_bps",
    "recent_return_bps",
    "short_vol_proxy_bps",
)

OB_FEATURES = (
    "imbalance_0_10",
    "imbalance_0_25",
    "support_frac_0",
    "break_side_near_depth",
    "support_near_depth",
    "support_minus_break_depth",
    "support_depth_0_5",
    "break_depth_0_5",
    "support_depth_change_10s",
    "flow_5s_signed_break",
    "flow_10s_signed_break",
    "flow_30s_signed_break",
)

# Fixed 3-feature scorecard — no in-sample weight search
SCORE_FEATURES = (
    "support_minus_break_depth",  # depth pressure
    "support_frac_0",  # directional imbalance proxy
    "flow_30s_signed_break",  # trade pressure
)


def _neg_labels(mode: str) -> set[str]:
    if mode == "vs_reclaim_fast":
        return {"RECLAIM_FAST"}
    if mode == "vs_rest":
        return {"RECLAIM_FAST", "RECLAIM_SLOW", "HOLD_NO_BREAK"}
    raise ValueError(mode)


def collect_event_values(
    rows: list[dict[str, Any]],
    *,
    subgroup: str,
    timepoint: str,
    feature: str,
    mode: str,
) -> list[tuple[str, float, str]]:
    """One value per event at timepoint; DROP events without value."""
    neg = _neg_labels(mode)
    by_event: dict[str, dict[str, Any]] = {}
    for r in rows:
        if r["timepoint"] != timepoint:
            continue
        if not event_in_subgroup(r["symbol"], r["break_direction"], subgroup):
            continue
        lab = r["outcome_label"]
        if lab != "BREAK_ACCEPTED" and lab not in neg:
            continue
        v = to_float(r.get(feature))
        if v is None:
            continue
        by_event[r["event_id"]] = {"score": v, "outcome": lab, "symbol": r["symbol"]}
    return [(eid, d["score"], d["outcome"]) for eid, d in sorted(by_event.items())]


def evaluate_feature(
    rows: list[dict[str, Any]],
    *,
    subgroup: str,
    timepoint: str,
    feature: str,
    mode: str,
) -> dict[str, Any] | None:
    pairs = collect_event_values(rows, subgroup=subgroup, timepoint=timepoint, feature=feature, mode=mode)
    pos = [s for _, s, y in pairs if y == "BREAK_ACCEPTED"]
    neg = [s for _, s, y in pairs if y != "BREAK_ACCEPTED"]
    n_break, n_other = len(pos), len(neg)
    sufficient = n_break >= MIN_N_BREAK and n_other >= MIN_N_OTHER
    auc, ori = best_auc(pos, neg) if sufficient else (None, None)
    ci = (
        bootstrap_auc_ci(pos, neg, orientation=ori or "higher→BREAK_ACCEPTED", seed=42)
        if sufficient and ori
        else {"auc": None, "ci_low": None, "ci_high": None, "orientation": ori}
    )
    window = "EARLY" if timepoint in EARLY_TIMEPOINTS else (
        "CONFIRMATION_ONLY" if timepoint in CONFIRMATION_ONLY_TIMEPOINTS else "OTHER"
    )
    return {
        "subgroup": subgroup,
        "timepoint": timepoint,
        "window": window,
        "feature": feature,
        "comparison": mode,
        "n_break": n_break,
        "n_other": n_other,
        "sufficient_sample": int(sufficient),
        "auc": ci["auc"] if sufficient else None,
        "auc_ci_low": ci.get("ci_low"),
        "auc_ci_high": ci.get("ci_high"),
        "orientation": ci.get("orientation") or ori,
        "median_break": None if not pos else sorted(pos)[len(pos) // 2],
        "median_other": None if not neg else sorted(neg)[len(neg) // 2],
        "median_diff_break_minus_other": median_diff(pos, neg),
        "cohens_d": cohens_d(pos, neg) if sufficient else None,
    }


def run_subgroup_feature_stats(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    tps = list(EARLY_TIMEPOINTS) + ["BREAK_PLUS_20S", "BREAK_PLUS_30S"]
    for sg in SUBGROUPS:
        for tp in tps:
            for feat in EARLY_FEATURES:
                for mode in ("vs_reclaim_fast", "vs_rest"):
                    ev = evaluate_feature(rows, subgroup=sg, timepoint=tp, feature=feat, mode=mode)
                    if ev is not None:
                        out.append(ev)
    return out


def _combo_scores(
    rows: list[dict[str, Any]],
    *,
    subgroup: str,
    timepoint: str,
    features: tuple[str, ...],
    mode: str,
    higher_is_break: list[bool] | None = None,
) -> list[tuple[str, float, str]]:
    """Build average-rank combo score per event."""
    neg = _neg_labels(mode)
    # gather feature matrix
    events: dict[str, dict[str, Any]] = {}
    for r in rows:
        if r["timepoint"] != timepoint:
            continue
        if not event_in_subgroup(r["symbol"], r["break_direction"], subgroup):
            continue
        lab = r["outcome_label"]
        if lab != "BREAK_ACCEPTED" and lab not in neg:
            continue
        events.setdefault(r["event_id"], {"outcome": lab, "vals": {}})
        for f in features:
            events[r["event_id"]]["vals"][f] = to_float(r.get(f))

    eids = sorted(events)
    matrix = [[events[e]["vals"].get(f) for f in features] for e in eids]
    # Default orientation from univariate best on available data
    if higher_is_break is None:
        higher_is_break = []
        for f in features:
            pos = [events[e]["vals"][f] for e in eids if events[e]["outcome"] == "BREAK_ACCEPTED" and events[e]["vals"].get(f) is not None]
            negv = [events[e]["vals"][f] for e in eids if events[e]["outcome"] != "BREAK_ACCEPTED" and events[e]["vals"].get(f) is not None]
            posf = [x for x in pos if x is not None]
            negf = [x for x in negv if x is not None]
            _, ori = best_auc(posf, negf)  # type: ignore[arg-type]
            higher_is_break.append(ori == "higher→BREAK_ACCEPTED" if ori else True)

    scores = average_rank_score(matrix, higher_is_break=higher_is_break)
    out = []
    for eid, sc in zip(eids, scores):
        if sc is None:
            continue
        out.append((eid, float(sc), events[eid]["outcome"]))
    return out


def evaluate_score_list(
    pairs: list[tuple[str, float, str]],
    *,
    subgroup: str,
    timepoint: str,
    feature: str,
    mode: str,
) -> dict[str, Any]:
    pos = [s for _, s, y in pairs if y == "BREAK_ACCEPTED"]
    neg = [s for _, s, y in pairs if y != "BREAK_ACCEPTED"]
    n_break, n_other = len(pos), len(neg)
    sufficient = n_break >= MIN_N_BREAK and n_other >= MIN_N_OTHER
    auc, ori = best_auc(pos, neg) if sufficient else (None, None)
    ci = (
        bootstrap_auc_ci(pos, neg, orientation=ori or "higher→BREAK_ACCEPTED", seed=42)
        if sufficient and ori
        else {"auc": None, "ci_low": None, "ci_high": None, "orientation": ori}
    )
    jk = jackknife_auc(pairs) if sufficient else {"full_auc": None, "max_drop": None, "loo_auc_min": None}
    window = "EARLY" if timepoint in EARLY_TIMEPOINTS else "CONFIRMATION_ONLY"
    return {
        "subgroup": subgroup,
        "timepoint": timepoint,
        "window": window,
        "feature": feature,
        "comparison": mode,
        "n_break": n_break,
        "n_other": n_other,
        "sufficient_sample": int(sufficient),
        "auc": ci.get("auc"),
        "auc_ci_low": ci.get("ci_low"),
        "auc_ci_high": ci.get("ci_high"),
        "orientation": ci.get("orientation") or ori,
        "median_diff_break_minus_other": median_diff(pos, neg),
        "cohens_d": cohens_d(pos, neg) if sufficient else None,
        "jackknife_full_auc": jk.get("full_auc"),
        "jackknife_loo_min": jk.get("loo_auc_min"),
        "jackknife_loo_mean": jk.get("loo_auc_mean"),
        "jackknife_max_drop": jk.get("max_drop"),
    }


def distance_baseline_comparison(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """AUC distance-only vs OB-only vs OB+distance for early timepoints / subgroups."""
    out = []
    for sg in SUBGROUPS:
        for tp in ("PRE_TOUCH_60S", "PRE_TOUCH_30S", "PRE_TOUCH_10S", "FIRST_TOUCH"):
            for mode in ("vs_reclaim_fast", "vs_rest"):
                # distance-only combo
                d_pairs = _combo_scores(
                    rows,
                    subgroup=sg,
                    timepoint=tp,
                    features=("abs_distance_to_level_bps", "signed_distance_beyond_bps", "recent_return_bps"),
                    mode=mode,
                )
                # OB-only
                o_pairs = _combo_scores(
                    rows,
                    subgroup=sg,
                    timepoint=tp,
                    features=("support_frac_0", "support_minus_break_depth", "flow_30s_signed_break"),
                    mode=mode,
                )
                # OB + distance
                c_pairs = _combo_scores(
                    rows,
                    subgroup=sg,
                    timepoint=tp,
                    features=(
                        "abs_distance_to_level_bps",
                        "signed_distance_beyond_bps",
                        "support_frac_0",
                        "support_minus_break_depth",
                        "flow_30s_signed_break",
                    ),
                    mode=mode,
                )
                for label, pairs in (
                    ("distance_only", d_pairs),
                    ("ob_only", o_pairs),
                    ("ob_plus_distance", c_pairs),
                ):
                    ev = evaluate_score_list(
                        pairs, subgroup=sg, timepoint=tp, feature=label, mode=mode
                    )
                    # also univariate signed_distance
                    out.append(ev)
                # univariate signed distance for reference
                uni = evaluate_feature(
                    rows,
                    subgroup=sg,
                    timepoint=tp,
                    feature="signed_distance_beyond_bps",
                    mode=mode,
                )
                if uni:
                    uni["feature"] = "signed_distance_beyond_bps_univariate"
                    out.append(uni)
    return out


def early_signal_results(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Fixed 2–3 feature scorecard at early times; plus best single early feature."""
    out = []
    for sg in SUBGROUPS:
        for tp in ("PRE_TOUCH_30S", "PRE_TOUCH_10S", "FIRST_TOUCH", "FIRST_BREAK", "BREAK_PLUS_5S", "BREAK_PLUS_10S"):
            for mode in ("vs_reclaim_fast", "vs_rest"):
                pairs = _combo_scores(
                    rows,
                    subgroup=sg,
                    timepoint=tp,
                    features=SCORE_FEATURES,
                    mode=mode,
                )
                ev = evaluate_score_list(
                    pairs,
                    subgroup=sg,
                    timepoint=tp,
                    feature="score_depth_imb_flow",
                    mode=mode,
                )
                out.append(ev)
    return out


def jackknife_table(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Jackknife for strongest early candidates per subgroup."""
    out = []
    for sg in SUBGROUPS:
        for tp in ("PRE_TOUCH_30S", "PRE_TOUCH_10S", "FIRST_TOUCH"):
            for mode in ("vs_reclaim_fast", "vs_rest"):
                for feat in ("score_depth_imb_flow", "imbalance_0_10", "support_frac_0", "signed_distance_beyond_bps"):
                    if feat == "score_depth_imb_flow":
                        pairs = _combo_scores(
                            rows, subgroup=sg, timepoint=tp, features=SCORE_FEATURES, mode=mode
                        )
                    else:
                        pairs = collect_event_values(
                            rows, subgroup=sg, timepoint=tp, feature=feat, mode=mode
                        )
                    pos = [s for _, s, y in pairs if y == "BREAK_ACCEPTED"]
                    neg = [s for _, s, y in pairs if y != "BREAK_ACCEPTED"]
                    if len(pos) < MIN_N_BREAK or len(neg) < MIN_N_OTHER:
                        continue
                    jk = jackknife_auc(pairs)
                    out.append(
                        {
                            "subgroup": sg,
                            "timepoint": tp,
                            "feature": feat,
                            "comparison": mode,
                            **jk,
                        }
                    )
    return out


def symbol_transfer(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Train orientation/threshold on one symbol; apply to the other without retuning."""
    out = []
    pairs_sym = (("APTUSDT", "DOGEUSDT"), ("DOGEUSDT", "APTUSDT"))
    for train_sym, test_sym in pairs_sym:
        for direction in ("bearish", "bullish"):
            train_sg = f"{'APT' if train_sym.startswith('APT') else 'DOGE'}_{direction}"
            test_sg = f"{'APT' if test_sym.startswith('APT') else 'DOGE'}_{direction}"
            for tp in ("PRE_TOUCH_30S", "PRE_TOUCH_10S", "FIRST_TOUCH"):
                for mode in ("vs_reclaim_fast", "vs_rest"):
                    for feat in ("score_depth_imb_flow", "imbalance_0_10", "support_frac_0", "signed_distance_beyond_bps"):
                        if feat == "score_depth_imb_flow":
                            train_pairs = _combo_scores(
                                rows, subgroup=train_sg, timepoint=tp, features=SCORE_FEATURES, mode=mode
                            )
                            # freeze orientations from train univariate components
                            # rebuild test with train orientations
                            higher = []
                            for f in SCORE_FEATURES:
                                tpairs = collect_event_values(
                                    rows, subgroup=train_sg, timepoint=tp, feature=f, mode=mode
                                )
                                pos = [s for _, s, y in tpairs if y == "BREAK_ACCEPTED"]
                                neg = [s for _, s, y in tpairs if y != "BREAK_ACCEPTED"]
                                _, ori = best_auc(pos, neg)
                                higher.append(ori == "higher→BREAK_ACCEPTED" if ori else True)
                            test_pairs = _combo_scores(
                                rows,
                                subgroup=test_sg,
                                timepoint=tp,
                                features=SCORE_FEATURES,
                                mode=mode,
                                higher_is_break=higher,
                            )
                        else:
                            train_pairs = collect_event_values(
                                rows, subgroup=train_sg, timepoint=tp, feature=feat, mode=mode
                            )
                            test_pairs = collect_event_values(
                                rows, subgroup=test_sg, timepoint=tp, feature=feat, mode=mode
                            )

                        tpos = [s for _, s, y in train_pairs if y == "BREAK_ACCEPTED"]
                        tneg = [s for _, s, y in train_pairs if y != "BREAK_ACCEPTED"]
                        if len(tpos) < MIN_N_BREAK or len(tneg) < MIN_N_OTHER:
                            out.append(
                                {
                                    "train_symbol": train_sym,
                                    "test_symbol": test_sym,
                                    "direction": direction,
                                    "timepoint": tp,
                                    "feature": feat,
                                    "comparison": mode,
                                    "status": "INSUFFICIENT_TRAIN",
                                    "train_auc": None,
                                    "test_auc_fixed_orientation": None,
                                    "threshold_train_mid": None,
                                    "test_hit_break": None,
                                    "test_hit_other": None,
                                }
                            )
                            continue
                        train_auc, ori = best_auc(tpos, tneg)
                        # threshold: midpoint of medians on train
                        import statistics

                        mid = (statistics.median(tpos) + statistics.median(tneg)) / 2.0
                        higher_break = ori == "higher→BREAK_ACCEPTED"

                        xpos = [s for _, s, y in test_pairs if y == "BREAK_ACCEPTED"]
                        xneg = [s for _, s, y in test_pairs if y != "BREAK_ACCEPTED"]
                        if higher_break:
                            test_auc = None if not xpos or not xneg else mann_whitney_auc(xpos, xneg)
                            hit_b = sum(1 for s in xpos if s >= mid) / len(xpos) if xpos else None
                            hit_o = sum(1 for s in xneg if s >= mid) / len(xneg) if xneg else None
                        else:
                            test_auc = None if not xpos or not xneg else mann_whitney_auc(xneg, xpos)
                            hit_b = sum(1 for s in xpos if s <= mid) / len(xpos) if xpos else None
                            hit_o = sum(1 for s in xneg if s <= mid) / len(xneg) if xneg else None

                        consistent = (
                            test_auc is not None
                            and train_auc is not None
                            and test_auc >= 0.55
                            and abs((train_auc or 0) - (test_auc or 0)) <= 0.25
                        )
                        out.append(
                            {
                                "train_symbol": train_sym,
                                "test_symbol": test_sym,
                                "direction": direction,
                                "timepoint": tp,
                                "feature": feat,
                                "comparison": mode,
                                "status": "OK",
                                "train_n_break": len(tpos),
                                "train_n_other": len(tneg),
                                "test_n_break": len(xpos),
                                "test_n_other": len(xneg),
                                "train_auc": train_auc,
                                "train_orientation": ori,
                                "test_auc_fixed_orientation": test_auc,
                                "threshold_train_mid": mid,
                                "test_hit_rate_break": hit_b,
                                "test_hit_rate_other": hit_o,
                                "direction_consistent": int(bool(consistent)),
                            }
                        )
    return out


def classify_subgroup(
    *,
    counts: dict[str, Any],
    early_rows: list[dict[str, Any]],
    distance_rows: list[dict[str, Any]],
    jackknife_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    sg = counts["subgroup"]
    if not counts.get("sufficient_vs_reclaim_fast") and not counts.get("sufficient_vs_rest"):
        return {
            "subgroup": sg,
            "classification": "INSUFFICIENT_SAMPLE",
            "reason": "n too small for BREAK vs RECLAIM_FAST/REST",
            "best_early_auc": None,
            "best_early_timepoint": None,
            "best_early_feature": None,
        }

    early = [
        r
        for r in early_rows
        if r["subgroup"] == sg
        and r["window"] == "EARLY"
        and r.get("sufficient_sample")
        and r.get("auc") is not None
        and r["comparison"] == "vs_reclaim_fast"
    ]
    # also consider univariate from feature stats for early window
    # (caller may pass combined)

    # Prefer pre-touch / touch over first-break for EARLY_GATE when choosing best
    def rank_key(r: dict[str, Any]) -> tuple:
        tp = r.get("timepoint") or ""
        early_bonus = 0 if tp in {"PRE_TOUCH_60S", "PRE_TOUCH_30S", "PRE_TOUCH_10S", "FIRST_TOUCH"} else 1
        # penalize pure distance features
        feat = r.get("feature") or ""
        dist_pen = 1 if "distance" in feat else 0
        return (early_bonus, dist_pen, -(r.get("auc") or 0))

    best = min(early, key=rank_key) if early else None
    # among early_bonus==0 prefer highest auc
    early_pref = [
        r
        for r in early
        if r.get("timepoint") in {"PRE_TOUCH_60S", "PRE_TOUCH_30S", "PRE_TOUCH_10S", "FIRST_TOUCH"}
        and "distance" not in (r.get("feature") or "")
    ]
    if early_pref:
        best = max(early_pref, key=lambda r: r.get("auc") or 0)
    elif early:
        best = max(early, key=lambda r: r.get("auc") or 0)

    # distance control: OB-only should beat distance-only or OB+dist clearly > distance
    dist = [
        r
        for r in distance_rows
        if r["subgroup"] == sg
        and r["timepoint"] in {"PRE_TOUCH_30S", "PRE_TOUCH_10S", "FIRST_TOUCH"}
        and r["comparison"] == "vs_reclaim_fast"
        and r.get("sufficient_sample")
    ]
    dist_only = [r for r in dist if r["feature"] == "distance_only"]
    ob_only = [r for r in dist if r["feature"] == "ob_only"]
    ob_plus = [r for r in dist if r["feature"] == "ob_plus_distance"]

    def best_auc_of(xs: list[dict[str, Any]]) -> float | None:
        xs2 = [r for r in xs if r.get("auc") is not None]
        return max((r["auc"] for r in xs2), default=None)

    d_auc = best_auc_of(dist_only)
    o_auc = best_auc_of(ob_only)
    c_auc = best_auc_of(ob_plus)

    jk = [
        r
        for r in jackknife_rows
        if r["subgroup"] == sg and r.get("full_auc") is not None and r["comparison"] == "vs_reclaim_fast"
    ]
    max_drop = max((r.get("max_drop") or 0 for r in jk), default=None)
    min_loo = min((r.get("loo_auc_min") or 1 for r in jk), default=None) if jk else None

    # confirmation-only: strong only at +20s+
    # (handled via window on early_rows)

    if best is None or (best.get("auc") or 0) < 0.60:
        return {
            "subgroup": sg,
            "classification": "WEAK_SIGNAL",
            "reason": "no early AUC>=0.60 with sufficient n",
            "best_early_auc": None if best is None else best.get("auc"),
            "best_early_timepoint": None if best is None else best.get("timepoint"),
            "best_early_feature": None if best is None else best.get("feature"),
            "distance_only_auc": d_auc,
            "ob_only_auc": o_auc,
            "ob_plus_distance_auc": c_auc,
        }

    # distance proxy check
    only_distance = (
        d_auc is not None
        and (o_auc is None or o_auc < d_auc - 0.02)
        and (c_auc is None or c_auc <= d_auc + 0.02)
        and (best.get("feature") in {"signed_distance_beyond_bps", "abs_distance_to_level_bps", "distance_to_level_bps"}
            or (best.get("auc") or 0) <= (d_auc or 0) + 0.03)
    )

    late_only = best["timepoint"] in {"BREAK_PLUS_20S", "BREAK_PLUS_30S", "BREAK_PLUS_60S"}
    # early windows include up to +10s
    early_ok = best["timepoint"] in EARLY_TIMEPOINTS

    unstable = max_drop is not None and max_drop > 0.12 and (min_loo is not None and min_loo < 0.55)

    if late_only or not early_ok:
        cls = "CONFIRMATION_ONLY"
        reason = "signal only after early gate window"
    elif only_distance:
        cls = "WEAK_SIGNAL"
        reason = "OB does not beat distance-only baseline"
    elif unstable:
        cls = "WEAK_SIGNAL"
        reason = f"jackknife unstable max_drop={max_drop}"
    elif best.get("auc_ci_low") is not None and float(best["auc_ci_low"]) < 0.55:
        cls = "WEAK_SIGNAL"
        reason = "AUC CI too wide / unstable lower bound"
    elif (best.get("auc") or 0) >= 0.70 and (o_auc is not None and o_auc >= 0.65) and not unstable:
        # require OB not dominated by distance
        if d_auc is not None and o_auc < d_auc - 0.02:
            cls = "WEAK_SIGNAL"
            reason = "OB does not beat distance-only baseline"
        else:
            cls = "EARLY_GATE_CANDIDATE"
            reason = "early AUC robust; OB adds beyond distance"
    elif (best.get("auc") or 0) >= 0.65 and early_ok:
        # borderline
        if (
            o_auc is not None
            and o_auc >= 0.60
            and (d_auc is None or o_auc >= d_auc - 0.05)
            and (best.get("auc_ci_low") is None or float(best["auc_ci_low"]) >= 0.55)
        ):
            cls = "EARLY_GATE_CANDIDATE"
            reason = "early AUC acceptable; OB competitive with distance"
        else:
            cls = "WEAK_SIGNAL"
            reason = "early AUC but distance/OB control fails"
    else:
        cls = "WEAK_SIGNAL"
        reason = "insufficient strength"

    return {
        "subgroup": sg,
        "classification": cls,
        "reason": reason,
        "best_early_auc": best.get("auc"),
        "best_early_timepoint": best.get("timepoint"),
        "best_early_feature": best.get("feature"),
        "best_early_ci_low": best.get("auc_ci_low"),
        "best_early_ci_high": best.get("auc_ci_high"),
        "distance_only_auc": d_auc,
        "ob_only_auc": o_auc,
        "ob_plus_distance_auc": c_auc,
        "jackknife_max_drop": max_drop,
        "jackknife_loo_min": min_loo,
    }


def decide_primary(classifications: list[dict[str, Any]], transfer: list[dict[str, Any]]) -> str:
    by = {c["subgroup"]: c for c in classifications}
    apt_b = by.get("APT_bearish", {})
    doge_b = by.get("DOGE_bearish", {})
    all_b = by.get("all_bearish", {})

    candidates = [c for c in classifications if c["classification"] == "EARLY_GATE_CANDIDATE"]
    if all(c["classification"] == "INSUFFICIENT_SAMPLE" for c in classifications):
        return "SUBGROUP_SAMPLE_INSUFFICIENT"

    def distance_dominated(c: dict[str, Any]) -> bool:
        d = c.get("distance_only_auc")
        o = c.get("ob_only_auc")
        if d is None or o is None:
            return "distance" in (c.get("reason") or "").lower()
        return o < d - 0.02

    bearish_groups = [all_b, doge_b, apt_b]
    bearish_distance_dom = [
        c
        for c in bearish_groups
        if c
        and c.get("classification") != "INSUFFICIENT_SAMPLE"
        and (
            distance_dominated(c)
            or (c.get("best_early_feature") or "").startswith("abs_distance")
            or (c.get("best_early_feature") or "").startswith("signed_distance")
        )
    ]

    # Core research question from prior audit: bearish early OB signal.
    if bearish_distance_dom and not any(
        c.get("classification") == "EARLY_GATE_CANDIDATE" and str(c.get("subgroup", "")).endswith("bearish")
        for c in classifications
    ):
        return "EARLY_SIGNAL_NOT_ROBUST_AFTER_DISTANCE_CONTROL"

    if apt_b.get("classification") == "EARLY_GATE_CANDIDATE" and doge_b.get("classification") != "EARLY_GATE_CANDIDATE":
        return "APT_SPECIFIC_EARLY_GATE_CANDIDATE"
    if doge_b.get("classification") == "EARLY_GATE_CANDIDATE" and apt_b.get("classification") != "EARLY_GATE_CANDIDATE":
        return "DOGE_SPECIFIC_EARLY_GATE_CANDIDATE"

    bear_transfer = [
        t
        for t in transfer
        if t.get("direction") == "bearish"
        and t.get("status") == "OK"
        and t.get("feature") == "score_depth_imb_flow"
        and t.get("timepoint") in {"PRE_TOUCH_30S", "PRE_TOUCH_10S", "FIRST_TOUCH"}
        and t.get("comparison") == "vs_reclaim_fast"
    ]
    transfer_ok = any(t.get("direction_consistent") for t in bear_transfer)

    if all_b.get("classification") == "EARLY_GATE_CANDIDATE" and transfer_ok and not distance_dominated(all_b):
        return "BEARISH_EARLY_GATE_CANDIDATE"
    if candidates and all(str(c.get("subgroup", "")).endswith("bearish") for c in candidates):
        if transfer_ok and not any(distance_dominated(c) for c in candidates):
            return "BEARISH_EARLY_GATE_CANDIDATE"
        return "EARLY_SIGNAL_NOT_ROBUST_AFTER_DISTANCE_CONTROL"

    if candidates:
        # only non-bearish candidates (e.g. bullish) — not the intended robust early gate
        return "EARLY_SIGNAL_NOT_ROBUST_AFTER_DISTANCE_CONTROL" if bearish_distance_dom else "CONFIRMATION_ONLY"

    if any(c["classification"] == "CONFIRMATION_ONLY" for c in classifications):
        return "CONFIRMATION_ONLY"
    if bearish_distance_dom:
        return "EARLY_SIGNAL_NOT_ROBUST_AFTER_DISTANCE_CONTROL"
    return "CONFIRMATION_ONLY"

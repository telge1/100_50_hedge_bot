"""Transparent pattern summaries — no PnL optimization."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from orderbook_analyse.l2_wall_attack_discovery.models import safe_float


def _quantiles(vals: list[float]) -> dict[str, float | None]:
    if not vals:
        return {k: None for k in ("p10", "p25", "p50", "p75", "p90", "mean")}
    s = sorted(vals)

    def q(p: float) -> float:
        return s[min(len(s) - 1, int(p * (len(s) - 1)))]

    return {
        "p10": q(0.10),
        "p25": q(0.25),
        "p50": q(0.50),
        "p75": q(0.75),
        "p90": q(0.90),
        "mean": sum(s) / len(s),
    }


def _bucket_t2d(x: float | None) -> str | None:
    if x is None:
        return None
    if x < 0.25:
        return "<0.25"
    if x < 0.50:
        return "0.25-0.50"
    if x < 1.0:
        return "0.50-1.00"
    if x < 2.0:
        return "1.00-2.00"
    return ">2.00"


def _bucket_refill(x: float | None) -> str | None:
    if x is None:
        return None
    if x <= 0:
        return "0"
    if x < 0.25:
        return "0-0.25"
    if x < 0.50:
        return "0.25-0.50"
    if x <= 1.0:
        return "0.50-1.00"
    return ">1.00"


def pattern_summaries(
    primary: list[dict[str, Any]],
    labels_60: dict[str, str],
    proxies_5s: dict[str, dict[str, Any]],
    contact_5s: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    feature_summary: list[dict[str, Any]] = []
    bucket_summary: list[dict[str, Any]] = []
    defense_vs_break: list[dict[str, Any]] = []
    abs_vs_flow: list[dict[str, Any]] = []
    pull_vs_td: list[dict[str, Any]] = []

    feats = [
        "depletion_ratio",
        "refill_ratio",
        "resilience_ratio",
        "trade_to_display_ratio",
        "price_response_per_notional",
        "attack_notional",
    ]

    by_class: dict[str, list[str]] = defaultdict(list)
    for ep in primary:
        aid = ep["attack_id"]
        by_class[labels_60.get(aid, "DATA_UNAVAILABLE")].append(aid)

    for cls, ids in sorted(by_class.items()):
        for f in feats:
            vals = []
            for aid in ids:
                p = proxies_5s.get(aid, {})
                c = contact_5s.get(aid, {})
                v = safe_float(p.get(f) if f in p else c.get(f))
                if v is not None:
                    vals.append(v)
            feature_summary.append({"resolution_class": cls, "feature": f, "n": len(vals), **_quantiles(vals)})

    # buckets vs class rates
    for ep in primary:
        aid = ep["attack_id"]
        p = proxies_5s.get(aid, {})
        cls = labels_60.get(aid, "DATA_UNAVAILABLE")
        bucket_summary.append(
            {
                "attack_id": aid,
                "symbol": ep["symbol"],
                "side": ep["side"],
                "resolution_class_60s": cls,
                "t2d_bucket": _bucket_t2d(safe_float(p.get("trade_to_display_ratio"))),
                "refill_bucket": _bucket_refill(safe_float(p.get("refill_ratio"))),
                "pull_proxy_5s": p.get("pull_proxy"),
                "absorption_proxy_5s": p.get("absorption_proxy"),
            }
        )

    def _rate(ids: list[str], target: set[str]) -> float | None:
        if not ids:
            return None
        return sum(1 for i in ids if labels_60.get(i) in target) / len(ids)

    def _feat_median(ids: list[str], key: str) -> float | None:
        vals = [safe_float(proxies_5s.get(i, {}).get(key)) for i in ids]
        vals = [v for v in vals if v is not None]
        if not vals:
            return None
        vals.sort()
        return vals[len(vals) // 2]

    defended = by_class.get("DEFENDED", [])
    breaks = by_class.get("CLEAN_BREAK_CONTINUATION", []) + by_class.get("BREAK_RECLAIM", [])
    defense_vs_break.append(
        {
            "comparison": "DEFENDED_vs_BREAK",
            "n_defended": len(defended),
            "n_break": len(breaks),
            "median_resilience_defended": _feat_median(defended, "resilience_ratio"),
            "median_resilience_break": _feat_median(breaks, "resilience_ratio"),
            "median_t2d_defended": _feat_median(defended, "trade_to_display_ratio"),
            "median_t2d_break": _feat_median(breaks, "trade_to_display_ratio"),
            "median_refill_defended": _feat_median(defended, "refill_ratio"),
            "median_refill_break": _feat_median(breaks, "refill_ratio"),
        }
    )

    absorbed = by_class.get("ABSORBED_REFILLED", [])
    flow_died = by_class.get("FLOW_DIED_NO_DEFENSE", [])
    abs_vs_flow.append(
        {
            "comparison": "ABSORPTION_vs_FLOW_DIED",
            "n_absorbed": len(absorbed),
            "n_flow_died": len(flow_died),
            "median_attack_notional_absorbed": _feat_median(absorbed, "traded_at_level_proxy"),
            "median_attack_notional_flow_died": _feat_median(flow_died, "traded_at_level_proxy"),
            "median_resilience_absorbed": _feat_median(absorbed, "resilience_ratio"),
            "median_resilience_flow_died": _feat_median(flow_died, "resilience_ratio"),
        }
    )

    pulled = by_class.get("PULLED_ON_CONTACT", []) + by_class.get("PULLED_BEFORE_CONTACT", [])
    pull_vs_td.append(
        {
            "comparison": "PULL_vs_TRADE_DEPLETION",
            "n_pulled": len(pulled),
            "median_t2d_pulled": _feat_median(pulled, "trade_to_display_ratio"),
            "median_depletion_pulled": _feat_median(pulled, "depletion_ratio"),
            "share_pull_proxy_true": _rate(pulled, set(labels_60.values())) if pulled else None,
        }
    )

    # class probability by t2d bucket
    by_bucket: dict[str, list[str]] = defaultdict(list)
    for row in bucket_summary:
        if row["t2d_bucket"]:
            by_bucket[row["t2d_bucket"]].append(row["attack_id"])
    for b, ids in sorted(by_bucket.items()):
        feature_summary.append(
            {
                "resolution_class": f"BUCKET_t2d_{b}",
                "feature": "class_rate",
                "n": len(ids),
                "p_defended": _rate(ids, {"DEFENDED"}),
                "p_clean_break": _rate(ids, {"CLEAN_BREAK_CONTINUATION"}),
                "p_break_reclaim": _rate(ids, {"BREAK_RECLAIM"}),
                "p_absorbed": _rate(ids, {"ABSORBED_REFILLED"}),
                "p_pulled": _rate(ids, {"PULLED_ON_CONTACT", "PULLED_BEFORE_CONTACT"}),
                "p_flow_died": _rate(ids, {"FLOW_DIED_NO_DEFENSE"}),
                "p10": None,
                "p25": None,
                "p50": None,
                "p75": None,
                "p90": None,
                "mean": None,
            }
        )

    return feature_summary, bucket_summary, defense_vs_break, abs_vs_flow, pull_vs_td

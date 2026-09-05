"""Pre-contact and contact causal feature tables."""

from __future__ import annotations

from typing import Any

from orderbook_analyse.l2_wall_attack_discovery import DECISION_CUTOFFS_S
from orderbook_analyse.l2_wall_attack_discovery.models import bps_between, safe_float
from orderbook_analyse.ob200_v3_raw_discovery.reconstruct import SampleRow


def _sample_at(samples: list[SampleRow], ts_ms: int) -> SampleRow | None:
    if not samples:
        return None
    lo, hi = 0, len(samples) - 1
    ans = None
    while lo <= hi:
        mid = (lo + hi) // 2
        if samples[mid].ts_ms <= ts_ms:
            ans = samples[mid]
            lo = mid + 1
        else:
            hi = mid - 1
    return ans


def pre_contact_features(episode: dict[str, Any], samples: list[SampleRow], trade_windows: list[dict[str, Any]]) -> dict[str, Any]:
    fc = episode.get("first_contact_at")
    approach = episode.get("approach_at")
    cutoff = approach or (fc - 1 if fc else None)
    s = _sample_at(samples, int(cutoff)) if cutoff else None
    pre5 = next((w for w in trade_windows if w.get("window") == "m5_0"), {})
    pre30 = next((w for w in trade_windows if w.get("window") == "m30_m10"), {})
    return {
        "attack_id": episode["attack_id"],
        "semantic_role": "causal_feature",
        "feature_available_at": "first_contact_at",
        "causal_cutoff_ms": 0,
        "symbol": episode["symbol"],
        "side": episode["side"],
        "wall_size_pre": episode.get("wall_size_at_contact"),
        "wall_notional_pre": episode.get("wall_notional_at_contact"),
        "wall_dist_bps_pre": episode.get("wall_dist_bps_at_contact"),
        "wall_dist_ticks_pre": episode.get("wall_dist_ticks_at_contact"),
        "spread_bps_pre": None if s is None else s.spread_bps,
        "imbalance_l10_pre": None if s is None else s.imbalance_l10,
        "approach_at": approach,
        "pre5_attack_notional": pre5.get("attack_side_notional"),
        "pre5_trades_present": pre5.get("trades_present"),
        "pre5_burstiness": pre5.get("burstiness"),
        "pre30_attack_notional": pre30.get("attack_side_notional"),
        "is_retest": episode.get("is_retest"),
        "n_retests_in_lifecycle": episode.get("n_retests_in_lifecycle"),
    }


def contact_features(
    episode: dict[str, Any],
    proxies: list[dict[str, Any]],
    trade_windows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    rows = []
    for cut in DECISION_CUTOFFS_S:
        h = max(cut, 1) if cut == 0 else cut
        # at cutoff 0s use 1s proxy as earliest post-contact snapshot
        proxy = next((p for p in proxies if int(p["horizon_s"]) == h), {})
        tw = next((w for w in trade_windows if w.get("window") == f"p0_{h}"), {})
        rows.append(
            {
                "attack_id": episode["attack_id"],
                "decision_cutoff_s": cut,
                "semantic_role": "causal_feature",
                "feature_available_at": f"first_contact_at+{cut}s",
                "causal_cutoff_ms": cut * 1000,
                "symbol": episode["symbol"],
                "side": episode["side"],
                "attack_notional": tw.get("attack_side_notional") or proxy.get("attack_side_notional"),
                "trade_count": tw.get("trade_count"),
                "trades_present": tw.get("trades_present"),
                "depletion_ratio": proxy.get("depletion_ratio"),
                "refill_ratio": proxy.get("refill_ratio"),
                "resilience_ratio": proxy.get("resilience_ratio"),
                "trade_to_display_ratio": proxy.get("trade_to_display_ratio"),
                "price_response_per_notional": proxy.get("price_response_per_notional"),
                "pull_proxy": proxy.get("pull_proxy"),
                "absorption_proxy": proxy.get("absorption_proxy"),
                "attribution_confidence": proxy.get("attribution_confidence"),
                "burstiness": tw.get("burstiness"),
            }
        )
    return rows

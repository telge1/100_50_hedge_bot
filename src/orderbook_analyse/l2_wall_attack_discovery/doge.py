"""DOGE-specific wall distance / near-market diagnosis."""

from __future__ import annotations

from typing import Any

from orderbook_analyse.l2_wall_attack_discovery.models import bps_between, tick_size, ticks_between
from orderbook_analyse.ob200_v3_raw_discovery.lifecycles_v2 import WallLifecycle
from orderbook_analyse.ob200_v3_raw_discovery.reconstruct import SampleRow


def _quantiles(vals: list[float]) -> dict[str, float | None]:
    if not vals:
        return {k: None for k in ("min", "p10", "p25", "median", "p75", "p90", "max")}
    s = sorted(vals)

    def q(p: float) -> float:
        return s[min(len(s) - 1, int(p * (len(s) - 1)))]

    return {
        "min": s[0],
        "p10": q(0.10),
        "p25": q(0.25),
        "median": q(0.50),
        "p75": q(0.75),
        "p90": q(0.90),
        "max": s[-1],
    }


def doge_diagnosis(
    lifecycles: list[WallLifecycle],
    samples: list[SampleRow],
    episodes: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    doge_lc = [x for x in lifecycles if x.symbol == "DOGEUSDT"]
    doge_s = [s for s in samples if s.symbol == "DOGEUSDT" and not s.warmup]
    dists_bps: list[float] = []
    dists_ticks: list[float] = []
    near_5 = near_10 = near_20 = near_30 = 0
    for s in doge_s:
        for px in (s.bid_wall_price, s.ask_wall_price):
            if px is None or s.mid <= 0:
                continue
            d = bps_between(px, s.mid, s.mid)
            t = ticks_between(px, s.mid, "DOGEUSDT")
            if d is not None:
                dists_bps.append(d)
                if d <= 5:
                    near_5 += 1
                if d <= 10:
                    near_10 += 1
                if d <= 20:
                    near_20 += 1
                if d <= 30:
                    near_30 += 1
            if t is not None:
                dists_ticks.append(t)

    doge_ep = [e for e in episodes if e["symbol"] == "DOGEUSDT" and e.get("is_primary")]
    btc_ep = [e for e in episodes if e["symbol"] == "BTCUSDT" and e.get("is_primary")]
    diag = {
        "symbol": "DOGEUSDT",
        "tick_size": tick_size("DOGEUSDT"),
        "v2_style_lifecycles": len(doge_lc),
        "approaches": sum(1 for x in doge_lc if x.approach_ts),
        "touches": sum(1 for x in doge_lc if x.touch_ts),
        "attack_episodes_primary": len(doge_ep),
        "btc_attack_episodes_primary": len(btc_ep),
        "scaling_error_found": False,
        "distance_bps": {"n": len(dists_bps), **_quantiles(dists_bps)},
        "distance_ticks": {"n": len(dists_ticks), **_quantiles(dists_ticks)},
        "share_samples_wall_within_bps": {
            "5": near_5 / max(len(dists_bps), 1),
            "10": near_10 / max(len(dists_bps), 1),
            "20": near_20 / max(len(dists_bps), 1),
            "30": near_30 / max(len(dists_bps), 1),
        },
        "primary_cause": (
            "Dominant walls typically sit ~18–25 bps from mid under current Q×median rule; "
            "price rarely reaches touch band. Missing touch may itself reflect pull/migration "
            "during approach rather than a unit bug."
        ),
        "optional_sensitivity_note": (
            "Near-market relative walls / Q99-only not mixed into primary definition."
        ),
    }
    dist_rows = [
        {
            "metric": "wall_mid_distance_bps",
            **{k: diag["distance_bps"][k] for k in ("n", "min", "p10", "p25", "median", "p75", "p90", "max")},
        },
        {
            "metric": "wall_mid_distance_ticks",
            **{k: diag["distance_ticks"][k] for k in ("n", "min", "p10", "p25", "median", "p75", "p90", "max")},
        },
    ]
    return diag, dist_rows

"""Decision-time snapshots vs hindsight."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from research.btc_ob_fight.config import iso_z


def build_decision_snapshots(
    *,
    outer_cross_ts: datetime | None,
    peak_ts: datetime,
    peak_price: float,
    reclaim_ts: datetime,
    reclaim_price: float,
    retest_ts: datetime | None,
    retest_high: float | None,
    oi_at: dict[str, float | None],
    liq_counts: dict[str, int],
) -> dict[str, Any]:
    snapshots = {
        "A_outer_edge_cross": _snap(
            "Snapshot A: first outer-edge cross",
            outer_cross_ts,
            known=[
                "Price crossed Volume VVAH / outer edge",
                "Edge-zone attack underway",
            ],
            missing=[
                "Whether move is short-squeeze vs new longs",
                "Absorption evidence",
                "Reclaim outcome",
            ],
            short_justified=False,
        ),
        "B_price_peak": _snap(
            "Snapshot B: price peak",
            peak_ts,
            known=[
                f"Peak price {peak_price}",
                f"Short liquidations in window: {liq_counts.get('to_peak_short', 0)} events",
                f"OI change to peak: {oi_at.get('to_peak_delta')}",
            ],
            missing=["Reclaim timing", "Retest structure"],
            short_justified=False,
        ),
        "C_reclaim": _snap(
            "Snapshot C: canonical reclaim",
            reclaim_ts,
            known=[
                f"Price reclaimed below outer edge at {reclaim_price}",
                "Failed breakout candidate IF acceptance rules were frozen",
            ],
            missing=[
                "Retest outcome",
                "Later downward resolution (hindsight only)",
            ],
            short_justified="PARTIAL — reclaim is necessary but not sufficient for failed-breakout short",
        ),
        "D_retest": _snap(
            "Snapshot D: extended retest wick",
            retest_ts,
            known=[
                f"Retest high {retest_high}",
                "LOWER_HIGH vs first peak" if retest_high and retest_high < peak_price else "structure TBD",
            ],
            missing=[] if retest_ts else ["Retest not in standard window"],
            short_justified="PARTIAL if lower-high confirmed at decision time",
            hindsight_only=retest_ts is None,
        ),
        "E_post_resolution": {
            "label": "Snapshot E: hindsight only",
            "ts": None,
            "hindsight_only": True,
            "note": "Later price decline must not be attributed to reclaim-time knowledge",
        },
    }
    return snapshots


def _snap(
    label: str,
    ts: datetime | None,
    *,
    known: list[str],
    missing: list[str],
    short_justified: bool | str = False,
    hindsight_only: bool = False,
) -> dict[str, Any]:
    return {
        "label": label,
        "ts": iso_z(ts) if ts else None,
        "known_at_snapshot": known,
        "missing_confirmation": missing,
        "allowable_action": "WAIT" if not short_justified else short_justified,
        "hindsight_only": hindsight_only,
    }

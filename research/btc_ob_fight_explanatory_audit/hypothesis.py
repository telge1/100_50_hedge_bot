"""Hypothesis evaluation matrix."""

from __future__ import annotations

from typing import Any


def build_hypothesis_matrix(ctx: dict[str, Any]) -> dict[str, Any]:
    short_liq = ctx.get("short_liq_quote_core", 0)
    buy_delta_attack = ctx.get("buy_delta_attack_window", 0)
    oi_attack_delta = ctx.get("oi_attack_delta")
    reclaim_count = ctx.get("canonical_reclaim_count", 0)
    retest_class = ctx.get("retest_class", "AMBIGUOUS")
    nearby_ask = ctx.get("nearby_ask_count", 0)
    ask_decreases = ctx.get("trade_associated_ask_decreases", 0)
    coverage_weak = ctx.get("ob_coverage_weak", True)

    def row(name: str, status: str, support: list[str], contradict: list[str], limits: list[str]) -> dict:
        return {
            "hypothesis": name,
            "status": status,
            "supporting_facts": support,
            "contradicting_facts": contradict,
            "coverage_limits": limits,
            "missing_for_confirmation": [],
        }

    hypotheses = [
        row(
            "PURE_NEW_BUYER_BREAKOUT",
            "CONTRADICTED" if short_liq > 100_000 else "INCONCLUSIVE",
            [f"Taker buy delta attack window +{buy_delta_attack:.0f}"],
            [f"Short liquidation quote ~{short_liq:.0f} USD in core window"],
            ["Cannot separate organic buyers from forced covers without trade IDs"],
        ),
        row(
            "SHORT_LIQUIDATION_DOMINANT_UP_MOVE",
            "PARTIALLY_SUPPORTED",
            [f"59 short-liq events, ~{short_liq:.0f} USD notional", "Price rose into peak with heavy short liq cluster pre-peak"],
            [f"Large positive taker buy delta (+{buy_delta_attack:.0f}) exceeds liq notional alone"],
            ["Temporal association only; no direct liq→trade ID"],
        ),
        row(
            "MIXED_SHORT_SQUEEZE_AND_NEW_LONGS",
            "SUPPORTED",
            [
                "Short liq + positive buy delta in attack",
                f"OI attack-window delta {oi_attack_delta} (short covering during breakout)",
                f"Full-window OI +114.33 suggests later long building",
            ],
            [],
            ["OI unit not physically confirmed as contracts"],
        ),
        row(
            "PASSIVE_SELLER_ABSORPTION",
            "INCONCLUSIVE",
            [f"{nearby_ask} nearby ask increases", f"{ask_decreases} trade-associated ask decreases"],
            ["Profile edge zone OB coverage mostly partial/outside book"],
            ["Insufficient observability at edge during attack — cannot confirm absorption"],
        ),
        row(
            "BUYER_EXHAUSTION_WITHOUT_PROVEN_ABSORPTION",
            "PARTIALLY_SUPPORTED",
            ["19:10–19:30 delta strongly negative while price failed to hold peak", "Buy delta collapsed post-peak"],
            ["Cannot rule out absorption due weak OB coverage"],
            [],
        ),
        row(
            "FAILED_BREAKOUT_AFTER_RECLAIM",
            "PARTIALLY_SUPPORTED",
            [f"{reclaim_count} canonical reclaims below outer edge", "Price returned below VVAH after peak"],
            ["Trading rules not frozen — research assessment only"],
            [],
        ),
        row(
            "FAILED_RETEST_LOWER_HIGH",
            "SUPPORTED" if retest_class == "LOWER_HIGH" else "INCONCLUSIVE",
            [f"Retest classification: {retest_class}", "Extended retest below first peak 79280.8"],
            ["Retest outside standard 19:30 decision window"],
            ["Extended window hindsight"],
        ),
        row(
            "SUSTAINED_BREAKOUT_ACCEPTANCE",
            "CONTRADICTED",
            [],
            ["Reclaim below outer edge", "19:00–19:30 net negative delta", retest_class],
            [],
        ),
    ]
    return {"hypotheses": hypotheses, "evaluation_policy": "No hypothesis confirmed by hindsight price alone"}

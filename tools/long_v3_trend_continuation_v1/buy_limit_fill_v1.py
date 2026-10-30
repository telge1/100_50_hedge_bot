"""Long buy-limit pending fill (mirror of causal_sell_limit_v2)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

FillModel = Literal["legacy_v1", "causal_buy_limit_v2"]

FILL_MODELS = frozenset({"legacy_v1", "causal_buy_limit_v2"})


@dataclass(frozen=True)
class BuyLimitFillDecision:
    filled: bool
    fill_price: float | None
    rule_id: str
    note: str
    ambiguous: bool = False
    ohlc_evidence: dict[str, float] | None = None


def evaluate_long_buy_limit_fill(
    *,
    fill_model: str,
    limit_price: float,
    bar_utc: str,
    order_active_from_utc: str,
    ohlc: dict[str, float],
) -> BuyLimitFillDecision:
    if bar_utc < order_active_from_utc:
        return BuyLimitFillDecision(
            filled=False,
            fill_price=None,
            rule_id="ORDER_NOT_ACTIVE",
            note="bar before order_active_from_utc",
        )

    o = float(ohlc["o"])
    h = float(ohlc["h"])
    l = float(ohlc["l"])
    lim = float(limit_price)
    evidence = {"o": o, "h": h, "l": l, "limit": lim}

    if fill_model == "legacy_v1":
        if l <= lim <= h:
            return BuyLimitFillDecision(
                filled=True,
                fill_price=lim,
                rule_id="LEGACY_V1_BRACKET_TOUCH",
                note="low <= limit <= high",
                ohlc_evidence=evidence,
            )
        return BuyLimitFillDecision(
            filled=False,
            fill_price=None,
            rule_id="LEGACY_V1_NO_BRACKET",
            note="limit outside bar range",
            ohlc_evidence=evidence,
        )

    if fill_model != "causal_buy_limit_v2":
        raise ValueError(f"unknown fill_model: {fill_model}")

    if l > lim:
        return BuyLimitFillDecision(
            filled=False,
            fill_price=None,
            rule_id="CAUSAL_V2_LOW_ABOVE_LIMIT",
            note="low > buy limit — no touch",
            ohlc_evidence=evidence,
        )

    if o <= lim:
        return BuyLimitFillDecision(
            filled=True,
            fill_price=o,
            rule_id="CAUSAL_V2_MARKETABLE_AT_OPEN",
            note="open <= buy limit — filled at open (conservative long)",
            ohlc_evidence=evidence,
        )

    if l <= lim:
        return BuyLimitFillDecision(
            filled=True,
            fill_price=lim,
            rule_id="CAUSAL_V2_LIMIT_TOUCH_INTRABAR",
            note="open > limit >= low — limit touched, fill at limit",
            ohlc_evidence=evidence,
        )

    return BuyLimitFillDecision(
        filled=False,
        fill_price=None,
        rule_id="CAUSAL_V2_NO_FILL",
        note="unreachable",
        ambiguous=True,
        ohlc_evidence=evidence,
    )

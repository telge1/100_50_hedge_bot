"""Short sell-limit pending fill models (BT-D7/BT-D18)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

FillModel = Literal["legacy_v1", "causal_sell_limit_v2"]

FILL_MODELS = frozenset({"legacy_v1", "causal_sell_limit_v2"})


@dataclass(frozen=True)
class SellLimitFillDecision:
    filled: bool
    fill_price: float | None
    rule_id: str
    note: str
    ambiguous: bool = False
    ohlc_evidence: dict[str, float] | None = None


def short_entry_fill_model(config: dict[str, Any]) -> str:
    se = config.get("short_entry") or {}
    model = str(se.get("pending_limit_fill_model") or "legacy_v1").strip()
    if model not in FILL_MODELS:
        raise ValueError(f"unsupported pending_limit_fill_model: {model}")
    return model


def evaluate_short_sell_limit_fill(
    *,
    fill_model: str,
    limit_price: float,
    bar_utc: str,
    order_active_from_utc: str,
    ohlc: dict[str, float],
) -> SellLimitFillDecision:
    """Evaluate one 5m bar for a resting short sell limit at limit_price."""
    if bar_utc < order_active_from_utc:
        return SellLimitFillDecision(
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
            return SellLimitFillDecision(
                filled=True,
                fill_price=lim,
                rule_id="LEGACY_V1_BRACKET_TOUCH",
                note="low <= limit <= high",
                ohlc_evidence=evidence,
            )
        return SellLimitFillDecision(
            filled=False,
            fill_price=None,
            rule_id="LEGACY_V1_NO_BRACKET",
            note="limit outside bar range",
            ohlc_evidence=evidence,
        )

    if fill_model != "causal_sell_limit_v2":
        raise ValueError(f"unknown fill_model: {fill_model}")

    if h < lim:
        return SellLimitFillDecision(
            filled=False,
            fill_price=None,
            rule_id="CAUSAL_V2_HIGH_BELOW_LIMIT",
            note="high < sell limit — no touch",
            ohlc_evidence=evidence,
        )

    if o >= lim:
        # Marketable sell limit at bar open: conservative short entry price = open (>= limit).
        return SellLimitFillDecision(
            filled=True,
            fill_price=o,
            rule_id="CAUSAL_V2_MARKETABLE_AT_OPEN",
            note="open >= sell limit — filled at open (conservative)",
            ohlc_evidence=evidence,
        )

    if h >= lim:
        return SellLimitFillDecision(
            filled=True,
            fill_price=lim,
            rule_id="CAUSAL_V2_LIMIT_TOUCH_INTRABAR",
            note="open < limit <= high — limit touched, fill at limit",
            ohlc_evidence=evidence,
        )

    return SellLimitFillDecision(
        filled=False,
        fill_price=None,
        rule_id="CAUSAL_V2_NO_FILL",
        note="unreachable",
        ambiguous=True,
        ohlc_evidence=evidence,
    )

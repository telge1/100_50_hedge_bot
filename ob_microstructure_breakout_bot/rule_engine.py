from __future__ import annotations

from ob_microstructure_breakout_bot.ema import classify_ema_exit, trend_still_intact
from ob_microstructure_breakout_bot.models import (
    BreakoutTier,
    ClassificationResult,
    CoinThresholds,
    EmaSnapshot,
    MarketState,
    ObBandSnapshot,
    TouchDirection,
    TradeWindowStats,
)


def _ratio_or_zero(ob: ObBandSnapshot | None) -> float:
    if ob is None:
        return 0.0
    ratio = ob.bid_ask_ratio_5bps
    return float(ratio) if ratio is not None else 0.0


def classify_long_breakout(
    *,
    thresholds: CoinThresholds,
    confirm: TradeWindowStats,
    followthrough: TradeWindowStats | None = None,
    ob_at_event: ObBandSnapshot | None = None,
    ema: EmaSnapshot | None = None,
) -> ClassificationResult:
    """Classify a long breakout candidate into tier 0 / 1 / 2."""
    delta = confirm.delta_notional
    ratio = _ratio_or_zero(ob_at_event)
    metrics = {
        "confirm_delta": delta,
        "confirm_buy": confirm.buy_notional,
        "confirm_sell": confirm.sell_notional,
        "ob_bid_ask_ratio_5bps": ratio,
        "ob_bid_5bps": None if ob_at_event is None else ob_at_event.bid_5bps,
        "ob_ask_5bps": None if ob_at_event is None else ob_at_event.ask_5bps,
    }
    reasons: list[str] = []

    # Hard fakeout: follow-through flips strongly negative
    if followthrough is not None:
        ft = followthrough.delta_notional
        metrics["followthrough_delta"] = ft
        if ft <= thresholds.fakeout_followthrough_flip_delta:
            reasons.append(
                f"follow-through delta {ft:.0f} <= "
                f"{thresholds.fakeout_followthrough_flip_delta:.0f}"
            )
            return ClassificationResult(
                state=MarketState.FAKEOUT,
                tier=BreakoutTier.FAKEOUT,
                side="long",
                reasons=reasons,
                metrics=metrics,
            )

    # Weak confirm → fakeout / chop bucket
    if delta < thresholds.tier1_min_confirm_delta:
        if delta <= thresholds.fakeout_max_confirm_delta and (
            ob_at_event is None or not ob_at_event.bid_dominant_5bps
        ):
            reasons.append(
                f"confirm delta {delta:.0f} too weak and no durable bid dominance"
            )
            return ClassificationResult(
                state=MarketState.FAKEOUT,
                tier=BreakoutTier.FAKEOUT,
                side="long",
                reasons=reasons,
                metrics=metrics,
            )
        reasons.append(f"confirm delta {delta:.0f} below tier1 min")
        return ClassificationResult(
            state=MarketState.CHOP,
            tier=BreakoutTier.FAKEOUT,
            side="long",
            reasons=reasons,
            metrics=metrics,
        )

    # Tier 2 strong:
    # - classic: large delta + strong OB ratio
    # - delta-led: large delta with at least tier1 supportive OB (bid dominant)
    if delta >= thresholds.tier2_min_confirm_delta and (
        ratio >= thresholds.tier2_min_bid_ask_ratio_5bps
        or (
            ratio >= thresholds.tier1_min_bid_ask_ratio_5bps
            and ob_at_event is not None
            and ob_at_event.bid_dominant_5bps
        )
    ):
        reasons.append("strong confirm delta with supportive OB")
        if ema is not None and trend_still_intact(ema):
            reasons.append(
                "EMA structure intact"
                + (" (EMA20 above EMA200)" if ema.ema200 is not None else "")
            )
        return ClassificationResult(
            state=MarketState.BREAKOUT_CONFIRMED,
            tier=BreakoutTier.STRONG,
            side="long",
            reasons=reasons,
            metrics=metrics,
        )

    # Tier 1 valid (moderate)
    if ratio >= thresholds.tier1_min_bid_ask_ratio_5bps or (
        ob_at_event is not None and ob_at_event.bid_dominant_5bps
    ):
        reasons.append("accepted confirm delta with supportive OB")
        if ema is not None and trend_still_intact(ema):
            reasons.append(
                "EMA structure still bullish"
                + (" (macro EMA200 bias)" if ema.ema200 is not None else "")
            )
        return ClassificationResult(
            state=MarketState.BREAKOUT_CONFIRMED,
            tier=BreakoutTier.VALID,
            side="long",
            reasons=reasons,
            metrics=metrics,
        )

    reasons.append("delta ok but OB not supportive enough — treat as chop/filter")
    return ClassificationResult(
        state=MarketState.CHOP,
        tier=BreakoutTier.FAKEOUT,
        side="long",
        reasons=reasons,
        metrics=metrics,
    )


def classify_ema59_touch(
    *,
    thresholds: CoinThresholds,
    direction: TouchDirection,
    context: TradeWindowStats,
    at_touch: TradeWindowStats,
    followthrough: TradeWindowStats | None,
    ob_at_touch: ObBandSnapshot | None,
    ema: EmaSnapshot | None,
    is_first_touch: bool = True,
    previous_ema: EmaSnapshot | None = None,
) -> ClassificationResult:
    """EMA59 touch = event trigger; classify after context + follow-through."""
    reasons = [
        f"EMA59 touch {direction.value}",
        f"context {thresholds.context_lookback_minutes}m delta={context.delta_notional:.0f}",
    ]
    if not is_first_touch:
        reasons.append("repeated retest — lower priority than first touch")

    metrics = {
        "touch_direction": direction.value,
        "context_delta": context.delta_notional,
        "touch_delta": at_touch.delta_notional,
        "is_first_touch": is_first_touch,
    }

    # Exit / hold path for longs when touching from above
    if ema is not None and direction == TouchDirection.FROM_ABOVE:
        sell_ft = followthrough is not None and followthrough.delta_notional < 0
        exit_res = classify_ema_exit(
            ema,
            previous=previous_ema,
            sell_followthrough=sell_ft,
            ob=ob_at_touch,
        )
        if exit_res is not None and exit_res.state in (
            MarketState.EXIT_WARNING,
            MarketState.EXIT_CONFIRMED,
            MarketState.HOLD,
        ):
            exit_res.reasons = reasons + exit_res.reasons
            exit_res.metrics = {**metrics, **exit_res.metrics}
            return exit_res

    # Hold / reclaim if bid absorption + structure intact (incl. EMA200 bias)
    if (
        ema is not None
        and trend_still_intact(ema)
        and ob_at_touch is not None
        and ob_at_touch.bid_dominant_5bps
        and (followthrough is None or followthrough.delta_notional >= 0)
    ):
        reasons.append("bid absorption + EMA structure intact — hold/reclaim")
        return ClassificationResult(
            state=MarketState.HOLD,
            side="long",
            reasons=reasons,
            metrics={
                **metrics,
                "ob_bid_5bps": ob_at_touch.bid_5bps,
                "ob_ask_5bps": ob_at_touch.ask_5bps,
            },
        )

    # Otherwise evaluate as breakout / fakeout using confirm = at_touch
    return classify_long_breakout(
        thresholds=thresholds,
        confirm=at_touch,
        followthrough=followthrough,
        ob_at_event=ob_at_touch,
        ema=ema,
    )


def classify_accumulation_box(
    *,
    thresholds: CoinThresholds,
    box_window: TradeWindowStats,
    breakout_confirm: TradeWindowStats | None,
    ob_near_low: ObBandSnapshot | None,
    ema: EmaSnapshot | None,
    price_above_box_high: bool = False,
) -> ClassificationResult:
    """Accumulation: do not enter mid-box; wait for breakout acceptance."""
    reasons: list[str] = []
    metrics = {
        "box_delta": box_window.delta_notional,
        "price_above_box_high": price_above_box_high,
    }

    bid_absorb = ob_near_low is not None and ob_near_low.bid_dominant_5bps
    stack_ok = ema is None or trend_still_intact(ema)
    durable_sell = (
        box_window.delta_notional <= thresholds.fakeout_followthrough_flip_delta
        and (ob_near_low is None or ob_near_low.ask_dominant_5bps)
        and not stack_ok
    )

    if durable_sell:
        reasons.append("durable sell expansion inside box — not accumulation")
        return ClassificationResult(
            state=MarketState.FAKEOUT,
            tier=BreakoutTier.FAKEOUT,
            side="long",
            reasons=reasons,
            metrics=metrics,
        )

    if not price_above_box_high:
        if bid_absorb and stack_ok:
            reasons.append("box hold with bid absorption — wait for breakout above box high")
            return ClassificationResult(
                state=MarketState.ACCUMULATION,
                side="long",
                reasons=reasons,
                metrics={
                    **metrics,
                    "ob_bid_5bps": None if ob_near_low is None else ob_near_low.bid_5bps,
                    "ob_ask_5bps": None if ob_near_low is None else ob_near_low.ask_5bps,
                },
            )
        reasons.append("inside box without clear bid absorption — setup only")
        return ClassificationResult(
            state=MarketState.SETUP,
            side="long",
            reasons=reasons,
            metrics=metrics,
        )

    if breakout_confirm is None:
        reasons.append("price above box high but no confirm window provided")
        return ClassificationResult(
            state=MarketState.SETUP,
            side="long",
            reasons=reasons,
            metrics=metrics,
        )

    reasons.append("box breakout acceptance — classify confirm strength")
    result = classify_long_breakout(
        thresholds=thresholds,
        confirm=breakout_confirm,
        ob_at_event=ob_near_low,
        ema=ema,
    )
    result.reasons = reasons + result.reasons
    return result


class RuleEngine:
    """Thin facade used by backtest / future live loop."""

    def __init__(self, thresholds: CoinThresholds) -> None:
        self.thresholds = thresholds

    def classify_long_breakout(self, **kwargs) -> ClassificationResult:
        return classify_long_breakout(thresholds=self.thresholds, **kwargs)

    def classify_ema59_touch(self, **kwargs) -> ClassificationResult:
        return classify_ema59_touch(thresholds=self.thresholds, **kwargs)

    def classify_accumulation_box(self, **kwargs) -> ClassificationResult:
        return classify_accumulation_box(thresholds=self.thresholds, **kwargs)

    def classify_ema_exit(self, ema: EmaSnapshot, **kwargs) -> ClassificationResult | None:
        return classify_ema_exit(ema, **kwargs)

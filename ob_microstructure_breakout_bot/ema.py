from __future__ import annotations

from ob_microstructure_breakout_bot.models import (
    ClassificationResult,
    EmaSnapshot,
    MarketState,
    ObBandSnapshot,
)


def _exit_metrics(current: EmaSnapshot) -> dict:
    return {
        "ema9": current.ema9,
        "ema20": current.ema20,
        "ema59": current.ema59,
        "ema200": current.ema200,
        "price": current.price,
        "bullish_stack": current.bullish_stack,
        "macro_long_bias": current.macro_long_bias,
    }


def is_ema200_noise_dip(
    current: EmaSnapshot,
    *,
    previous: EmaSnapshot | None = None,
) -> bool:
    """True for brief EMA200 sweeps that should not kill the long trend.

    Matches chart cases where:
    - price wicked below EMA200, or
    - EMA9 briefly crossed under EMA200 for one bar,
    while EMA20 stayed above EMA200 and did not lose EMA59.
    """
    if current.ema200 is None or not current.macro_long_bias:
        return False

    price_sweep = current.price_below_ema200
    brief_ema9_cross = (
        current.ema9_below_ema200
        and previous is not None
        and previous.ema200 is not None
        and previous.ema9 >= previous.ema200
    )
    return bool(price_sweep or brief_ema9_cross)


def classify_ema_exit(
    current: EmaSnapshot,
    *,
    previous: EmaSnapshot | None = None,
    sell_followthrough: bool = False,
    ob: ObBandSnapshot | None = None,
) -> ClassificationResult | None:
    """Exit hierarchy for riding the long trend until structure really breaks.

    Hard breaks (exit confirmed):
    - EMA20 < EMA59 (often also implies EMA20 lost EMA200)
    - EMA59 < EMA200

    Noise to ignore (hold):
    - price briefly below EMA200 while EMA20 stays above EMA200 / EMA59
    - single-bar EMA9 dip under EMA200 while EMA20 holds
    """
    reasons: list[str] = []
    metrics = _exit_metrics(current)
    ask_heavy = ob is not None and ob.ask_dominant_5bps

    # --- hard structure breaks ---
    if current.ema20_below_ema59:
        reasons.append("EMA20 crossed below EMA59 — trend break confirmed")
        if current.ema200 is not None and current.ema20 < current.ema200:
            reasons.append("EMA20 also below EMA200")
        if sell_followthrough:
            reasons.append("sell follow-through present")
        if ask_heavy:
            reasons.append("ask-dominant OB present")
        return ClassificationResult(
            state=MarketState.EXIT_CONFIRMED,
            side="long",
            reasons=reasons,
            metrics=metrics,
        )

    if current.ema200 is not None and current.ema59 < current.ema200:
        reasons.append("EMA59 crossed below EMA200 — macro trend broken")
        if sell_followthrough or ask_heavy:
            if sell_followthrough:
                reasons.append("sell follow-through confirms exit")
            if ask_heavy:
                reasons.append("ask-dominant OB confirms exit")
        return ClassificationResult(
            state=MarketState.EXIT_CONFIRMED,
            side="long",
            reasons=reasons,
            metrics=metrics,
        )

    # --- EMA200 noise: keep riding the trend ---
    if is_ema200_noise_dip(current, previous=previous):
        reasons.append(
            "EMA200 sweep / brief EMA9 dip ignored — EMA20 still above EMA200 and EMA59"
        )
        return ClassificationResult(
            state=MarketState.HOLD,
            side="long",
            reasons=reasons,
            metrics=metrics,
        )

    # price under EMA200 but EMA9/20 still clearly above (deep wick / liquidation)
    if (
        current.ema200 is not None
        and current.price_below_ema200
        and current.ema20 > current.ema200
        and current.ema9 > current.ema200
        and not current.ema20_below_ema59
    ):
        reasons.append(
            "price swept EMA200 but EMA9/EMA20 still above — treat as liquidation noise"
        )
        return ClassificationResult(
            state=MarketState.HOLD,
            side="long",
            reasons=reasons,
            metrics=metrics,
        )

    # --- legacy early weakness (no EMA200 protection or stack already soft) ---
    if current.ema9_below_ema59:
        # If macro long bias still intact, do not escalate on EMA9 alone
        if current.macro_long_bias and current.ema200 is not None:
            reasons.append(
                "EMA9 soft vs EMA59 but EMA20 still above EMA200/EMA59 — hold trend"
            )
            return ClassificationResult(
                state=MarketState.HOLD,
                side="long",
                reasons=reasons,
                metrics=metrics,
            )
        reasons.append("EMA9 crossed below EMA59 — early weakness")
        if sell_followthrough and current.price_below_ema59:
            reasons.append(
                "price failed reclaim + sell follow-through — close without waiting for EMA20"
            )
            return ClassificationResult(
                state=MarketState.EXIT_CONFIRMED,
                side="long",
                reasons=reasons,
                metrics=metrics,
            )
        return ClassificationResult(
            state=MarketState.EXIT_WARNING,
            side="long",
            reasons=reasons,
            metrics=metrics,
        )

    if previous is not None and previous.bullish_stack and not current.bullish_stack:
        if current.macro_long_bias and current.ema200 is not None:
            reasons.append("local stack soft but macro EMA200 bias intact — hold")
            return ClassificationResult(
                state=MarketState.HOLD,
                side="long",
                reasons=reasons,
                metrics=metrics,
            )
        reasons.append("bullish EMA stack lost (EMA9 > EMA20 > EMA59 broken)")
        return ClassificationResult(
            state=MarketState.EXIT_WARNING,
            side="long",
            reasons=reasons,
            metrics=metrics,
        )

    if current.price_below_ema59 and current.bullish_stack:
        reasons.append("price briefly below EMA59 but EMA9 > EMA20 > EMA59 intact — hold")
        return ClassificationResult(
            state=MarketState.HOLD,
            side="long",
            reasons=reasons,
            metrics=metrics,
        )

    return None


def trend_still_intact(ema: EmaSnapshot) -> bool:
    """Stay long while medium structure holds.

    Without EMA200: classic EMA9 > EMA20 > EMA59.
    With EMA200: ride as long as EMA20 > EMA200 and EMA20 > EMA59
    (brief EMA9 / price dips to EMA200 are allowed).
    """
    if ema.ema200 is None:
        return ema.bullish_stack
    return ema.macro_long_bias

"""Research-only early-downtrend state ladder (D1–D4).

Causal 5m closed-candle detectors. No live/pipeline integration.
``07:30 UTC`` is never used as a trading rule — only optional analysis anchors
may reference it for post-hoc cumulative returns.

States (shared vocabulary)
--------------------------
- neutral
- bearish_warning
- early_bearish_trend
- confirmed_bearish_trend

Variants differ only in how many concurrent criteria are required to enter /
hold each state and when a long block becomes active.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal

import pandas as pd

EarlyVariant = Literal["D1", "D2", "D3", "D4"]
EarlyState = Literal[
    "neutral",
    "bearish_warning",
    "early_bearish_trend",
    "confirmed_bearish_trend",
]


@dataclass(frozen=True)
class EarlyDowntrendConfig:
    variant: EarlyVariant = "D2"
    enabled: bool = False  # research overlay; never enable in live
    # Descriptive research thresholds (not fitted to March morning).
    min_neg_impulse_atr: float = 0.35
    min_lower_closes: int = 2
    warning_min_criteria: int = 2
    early_min_criteria: int = 3
    confirmed_min_criteria: int = 4
    min_hold_bars_early: int = 1
    min_hold_bars_confirmed: int = 2
    require_15m_not_bullish_for_confirm: bool = False
    block_on: Literal["warning", "early", "confirmed"] = "early"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def default_early_downtrend_config(*, variant: EarlyVariant = "D2") -> EarlyDowntrendConfig:
    """Return unchanged research defaults per variant (no threshold search)."""
    if variant == "D1":
        # Earliest / most sensitive: block once early_bearish_trend appears.
        return EarlyDowntrendConfig(
            variant="D1",
            enabled=False,
            warning_min_criteria=2,
            early_min_criteria=3,
            confirmed_min_criteria=4,
            min_hold_bars_early=0,
            min_hold_bars_confirmed=1,
            min_lower_closes=2,
            min_neg_impulse_atr=0.25,
            require_15m_not_bullish_for_confirm=False,
            block_on="early",
        )
    if variant == "D2":
        # Balanced research default.
        return EarlyDowntrendConfig(
            variant="D2",
            enabled=False,
            warning_min_criteria=2,
            early_min_criteria=4,
            confirmed_min_criteria=5,
            min_hold_bars_early=1,
            min_hold_bars_confirmed=2,
            min_lower_closes=2,
            min_neg_impulse_atr=0.35,
            require_15m_not_bullish_for_confirm=False,
            block_on="early",
        )
    if variant == "D3":
        # Structure-first: need HL/swing breaks before early; block on early.
        return EarlyDowntrendConfig(
            variant="D3",
            enabled=False,
            warning_min_criteria=2,
            early_min_criteria=4,
            confirmed_min_criteria=5,
            min_hold_bars_early=1,
            min_hold_bars_confirmed=2,
            min_lower_closes=3,
            min_neg_impulse_atr=0.35,
            require_15m_not_bullish_for_confirm=False,
            block_on="early",
        )
    if variant == "D4":
        # Strict: confirmed only (+ optional 15m not bullish); block on confirmed.
        return EarlyDowntrendConfig(
            variant="D4",
            enabled=False,
            warning_min_criteria=3,
            early_min_criteria=4,
            confirmed_min_criteria=5,
            min_hold_bars_early=2,
            min_hold_bars_confirmed=2,
            min_lower_closes=3,
            min_neg_impulse_atr=0.45,
            require_15m_not_bullish_for_confirm=True,
            block_on="confirmed",
        )
    raise ValueError(f"unknown early-downtrend variant: {variant!r}")


def _finite(v: object) -> float | None:
    try:
        x = float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if x != x:  # NaN
        return None
    return x


def _is_bullish_regime(label: object) -> bool:
    return "bull" in str(label or "").lower()


def _is_bearish_regime(label: object) -> bool:
    return "bear" in str(label or "").lower()


def compute_bar_criteria(
    row: dict[str, Any],
    *,
    prev_close: float | None,
    last_swing_high: float | None,
    last_swing_low: float | None,
    prior_swing_low: float | None,
    consecutive_lower_closes: int,
    lower_high_confirmed: bool,
    hl_broken: bool,
    swing_low_broken: bool,
    cfg: EarlyDowntrendConfig,
) -> dict[str, Any]:
    """Boolean criteria available from closed-bar information only."""
    close = _finite(row.get("close"))
    ema9 = _finite(row.get("ema_9"))
    ema20 = _finite(row.get("ema_20"))
    ema9_slope = _finite(row.get("ema9_slope") or row.get("ema_9_slope_3_pct"))
    ema20_slope = _finite(row.get("ema20_slope") or row.get("ema_20_slope_3_pct"))
    di_spread = _finite(row.get("di_spread"))
    adx = _finite(row.get("adx"))
    atr = _finite(row.get("atr"))
    regime_15m = row.get("regime_15m")

    close_lt_ema9 = close is not None and ema9 is not None and close < ema9
    close_lt_ema20 = close is not None and ema20 is not None and close < ema20
    ema9_slope_neg = ema9_slope is not None and ema9_slope < 0
    ema20_slope_neg = ema20_slope is not None and ema20_slope < 0
    di_bearish = di_spread is not None and di_spread < 0
    neg_impulse = False
    impulse_atr = None
    if close is not None and prev_close is not None and atr and atr > 0:
        impulse_atr = (prev_close - close) / atr
        neg_impulse = impulse_atr >= cfg.min_neg_impulse_atr
    lower_closes_ok = consecutive_lower_closes >= cfg.min_lower_closes
    regime_15m_not_bull = not _is_bullish_regime(regime_15m)
    regime_15m_bear = _is_bearish_regime(regime_15m)

    # Variant-specific criterion bags
    warning_flags = {
        "close_lt_ema9": close_lt_ema9,
        "ema9_slope_neg": ema9_slope_neg,
        "di_bearish": di_bearish,
        "neg_impulse": neg_impulse,
        "close_lt_ema20": close_lt_ema20,
    }
    early_flags = {
        **warning_flags,
        "hl_broken": hl_broken,
        "swing_low_broken": swing_low_broken,
        "lower_closes_ok": lower_closes_ok,
        "ema20_slope_neg": ema20_slope_neg,
    }
    confirm_flags = {
        **early_flags,
        "lower_high_confirmed": lower_high_confirmed,
        "regime_15m_not_bull": regime_15m_not_bull,
        "regime_15m_bear": regime_15m_bear,
    }

    # D3 emphasizes structure for early; D4 emphasizes 15m + more confirms.
    if cfg.variant == "D3":
        # Early requires at least one structure break among counted criteria.
        early_structure_gate = hl_broken or swing_low_broken
    else:
        early_structure_gate = True
    if cfg.variant == "D1":
        # D1 may count softer early without EMA20 requirement in the min count.
        pass

    def _count(flags: dict[str, bool], keys: list[str]) -> tuple[int, list[str]]:
        active = [k for k in keys if flags.get(k)]
        return len(active), active

    if cfg.variant == "D1":
        w_keys = ["close_lt_ema9", "ema9_slope_neg", "di_bearish", "neg_impulse"]
        e_keys = w_keys + ["hl_broken", "swing_low_broken", "lower_closes_ok", "close_lt_ema20"]
        c_keys = e_keys + ["lower_high_confirmed", "ema20_slope_neg"]
    elif cfg.variant == "D2":
        w_keys = ["close_lt_ema9", "close_lt_ema20", "ema9_slope_neg", "di_bearish"]
        e_keys = w_keys + ["hl_broken", "swing_low_broken", "neg_impulse", "lower_closes_ok"]
        c_keys = e_keys + ["lower_high_confirmed", "ema20_slope_neg"]
    elif cfg.variant == "D3":
        w_keys = ["close_lt_ema9", "ema9_slope_neg", "di_bearish", "lower_closes_ok"]
        e_keys = ["hl_broken", "swing_low_broken", "close_lt_ema20", "ema9_slope_neg", "di_bearish", "neg_impulse"]
        c_keys = e_keys + ["lower_high_confirmed", "ema20_slope_neg", "regime_15m_not_bull"]
    else:  # D4
        w_keys = ["close_lt_ema9", "close_lt_ema20", "ema9_slope_neg", "di_bearish", "neg_impulse"]
        e_keys = w_keys + ["hl_broken", "swing_low_broken", "lower_closes_ok"]
        c_keys = e_keys + ["lower_high_confirmed", "ema20_slope_neg", "regime_15m_not_bull"]

    w_n, w_active = _count(warning_flags if cfg.variant != "D3" else {**warning_flags, **early_flags}, w_keys)
    e_n, e_active = _count(early_flags, e_keys)
    c_n, c_active = _count(confirm_flags, c_keys)

    warning_hit = w_n >= cfg.warning_min_criteria
    early_hit = e_n >= cfg.early_min_criteria and early_structure_gate
    if cfg.variant == "D3":
        early_hit = early_hit and (hl_broken or swing_low_broken)
    confirm_hit = c_n >= cfg.confirmed_min_criteria
    if cfg.require_15m_not_bullish_for_confirm:
        confirm_hit = confirm_hit and regime_15m_not_bull

    return {
        "close": close,
        "ema_9": ema9,
        "ema_20": ema20,
        "ema9_slope": ema9_slope,
        "ema20_slope": ema20_slope,
        "di_spread": di_spread,
        "adx": adx,
        "atr": atr,
        "impulse_atr": impulse_atr,
        "neg_impulse": neg_impulse,
        "close_lt_ema9": close_lt_ema9,
        "close_lt_ema20": close_lt_ema20,
        "ema9_slope_neg": ema9_slope_neg,
        "ema20_slope_neg": ema20_slope_neg,
        "di_bearish": di_bearish,
        "consecutive_lower_closes": consecutive_lower_closes,
        "lower_closes_ok": lower_closes_ok,
        "hl_broken": hl_broken,
        "swing_low_broken": swing_low_broken,
        "lower_high_confirmed": lower_high_confirmed,
        "last_swing_high": last_swing_high,
        "last_swing_low": last_swing_low,
        "prior_swing_low": prior_swing_low,
        "regime_15m": regime_15m,
        "regime_15m_not_bull": regime_15m_not_bull,
        "regime_15m_bear": regime_15m_bear,
        "warning_criteria_count": w_n,
        "early_criteria_count": e_n,
        "confirmed_criteria_count": c_n,
        "warning_active_criteria": w_active,
        "early_active_criteria": e_active,
        "confirmed_active_criteria": c_active,
        "warning_hit": warning_hit,
        "early_hit": early_hit,
        "confirm_hit": confirm_hit,
    }


@dataclass
class _Runtime:
    state: EarlyState = "neutral"
    age: int = 0
    entry_reason: str | None = None


def step_state(rt: _Runtime, crit: dict[str, Any], cfg: EarlyDowntrendConfig) -> tuple[_Runtime, dict[str, Any]]:
    """Advance latching state; no flicker without criteria failure + age rules."""
    prev = rt.state
    nxt = prev
    reason = None

    if crit["confirm_hit"]:
        if prev != "confirmed_bearish_trend":
            # Enter confirmed either fresh or from early after min hold.
            if prev == "early_bearish_trend" and rt.age >= cfg.min_hold_bars_confirmed - 1:
                nxt = "confirmed_bearish_trend"
                reason = "CONFIRM_CRITERIA"
            elif prev in {"neutral", "bearish_warning"} and cfg.variant in {"D1", "D2"}:
                # Allow skip to confirmed only if enough criteria (still causal).
                if crit["confirmed_criteria_count"] >= cfg.confirmed_min_criteria + 1:
                    nxt = "confirmed_bearish_trend"
                    reason = "CONFIRM_SKIP"
                elif crit["early_hit"]:
                    nxt = "early_bearish_trend"
                    reason = "EARLY_CRITERIA"
            elif prev == "early_bearish_trend":
                nxt = "confirmed_bearish_trend"
                reason = "CONFIRM_CRITERIA"
            elif crit["early_hit"]:
                nxt = "early_bearish_trend"
                reason = "EARLY_BEFORE_CONFIRM"
        else:
            nxt = "confirmed_bearish_trend"
    elif crit["early_hit"]:
        if prev == "confirmed_bearish_trend":
            # Stay confirmed unless early fully fails (handled below).
            nxt = "confirmed_bearish_trend"
        elif prev == "early_bearish_trend":
            nxt = "early_bearish_trend"
        else:
            nxt = "early_bearish_trend"
            reason = "EARLY_CRITERIA"
    elif crit["warning_hit"]:
        if prev in {"early_bearish_trend", "confirmed_bearish_trend"}:
            # Soft downgrade only if early criteria lost.
            if not crit["early_hit"]:
                nxt = "bearish_warning"
                reason = "DOWNGRADE_TO_WARNING"
            else:
                nxt = prev
        else:
            nxt = "bearish_warning"
            reason = "WARNING_CRITERIA"
    else:
        if prev == "confirmed_bearish_trend" and not crit["early_hit"]:
            nxt = "bearish_warning" if crit["warning_hit"] else "neutral"
            reason = "EXIT_CONFIRMED"
        elif prev == "early_bearish_trend" and not crit["early_hit"]:
            nxt = "bearish_warning" if crit["warning_hit"] else "neutral"
            reason = "EXIT_EARLY"
        elif prev == "bearish_warning" and not crit["warning_hit"]:
            nxt = "neutral"
            reason = "EXIT_WARNING"
        else:
            nxt = "neutral" if prev == "neutral" else prev

    age = rt.age + 1 if nxt == prev else 0
    # Enforce min hold before promoting early→ from warning already done via criteria.
    if nxt == "early_bearish_trend" and prev == "bearish_warning" and cfg.min_hold_bars_early > 0:
        # Allow immediate early on first hit (age 0); min_hold applies to confirmed promotion.
        pass

    new_rt = _Runtime(state=nxt, age=age, entry_reason=reason or rt.entry_reason)
    block = False
    if cfg.block_on == "warning":
        block = nxt in {"bearish_warning", "early_bearish_trend", "confirmed_bearish_trend"}
    elif cfg.block_on == "early":
        block = nxt in {"early_bearish_trend", "confirmed_bearish_trend"}
    else:
        block = nxt == "confirmed_bearish_trend"

    meta = {
        "prev_state": prev,
        "state": nxt,
        "state_age": age,
        "transition_reason": reason,
        "would_block_long": block,
        "bearish_warning": nxt == "bearish_warning",
        "early_bearish_trend": nxt == "early_bearish_trend",
        "confirmed_bearish_trend": nxt == "confirmed_bearish_trend",
    }
    return new_rt, meta


def _to_utc_ts(value: object) -> pd.Timestamp:
    t = pd.Timestamp(value)
    if t.tzinfo is None:
        return t.tz_localize("UTC")
    return t.tz_convert("UTC")


def run_early_downtrend_timeline(
    frame: pd.DataFrame,
    cfg: EarlyDowntrendConfig,
    *,
    pivots: list[Any] | None = None,
    start: object | None = None,
    end: object | None = None,
) -> pd.DataFrame:
    """Walk closed 5m bars; emit one row per decision_time in ``[start, end]`` if set.

    Warm state from bars before ``start``. Pivots must expose confirmation_timestamp
    and only be used when confirmation_timestamp <= decision_time.
    """
    if frame.empty:
        return pd.DataFrame()
    f = frame.copy()
    f["timestamp"] = pd.to_datetime(f["timestamp"], utc=True)
    if "decision_time" not in f.columns:
        f["decision_time"] = f["timestamp"] + pd.Timedelta(minutes=5)
    else:
        f["decision_time"] = pd.to_datetime(f["decision_time"], utc=True)
    f = f.sort_values("decision_time").reset_index(drop=True)

    start_ts = _to_utc_ts(start) if start is not None else None
    end_ts = _to_utc_ts(end) if end is not None else None

    pivot_rows: list[dict[str, Any]] = []
    for p in pivots or []:
        if hasattr(p, "pivot_type"):
            pivot_rows.append(
                {
                    "pivot_type": p.pivot_type,
                    "price": p.price,
                    "confirmation_timestamp": pd.Timestamp(p.confirmation_timestamp),
                    "pivot_timestamp": pd.Timestamp(p.pivot_timestamp),
                }
            )
        elif isinstance(p, dict):
            pivot_rows.append(p)
    piv = pd.DataFrame(pivot_rows)
    if len(piv):
        piv["confirmation_timestamp"] = pd.to_datetime(piv["confirmation_timestamp"], utc=True)

    rt = _Runtime()
    prev_close: float | None = None
    consecutive_lower = 0
    rows: list[dict[str, Any]] = []

    # Track swing sequence for HL / LH
    last_high = last_low = None
    prior_low = None
    prev_high = None
    hl_broken = False
    swing_low_broken = False
    lower_high_confirmed = False

    for _, row in f.iterrows():
        dts = pd.Timestamp(row["decision_time"])
        if end_ts is not None and dts > end_ts:
            break

        # Causal pivots
        if len(piv):
            known = piv[piv["confirmation_timestamp"] <= dts]
            highs = known[known["pivot_type"].astype(str).str.contains("high", case=False, na=False)]
            lows = known[known["pivot_type"].astype(str).str.contains("low", case=False, na=False)]
            if len(highs):
                h = highs.sort_values("confirmation_timestamp").iloc[-1]
                new_high = float(h["price"])
                if last_high is not None and new_high < last_high:
                    lower_high_confirmed = True
                if last_high is not None:
                    prev_high = last_high
                last_high = new_high
            if len(lows):
                l = lows.sort_values("confirmation_timestamp").iloc[-1]
                new_low = float(l["price"])
                if last_low is not None:
                    prior_low = last_low
                    # If previous low was higher than one before, and we break it → HL break
                last_low = new_low

        close = _finite(row.get("close"))
        if close is not None and last_low is not None and close < last_low:
            swing_low_broken = True
            # If we had a higher-low structure (last_low > prior_low), breaking last_low is HL break
            if prior_low is not None and last_low > prior_low:
                hl_broken = True
            elif prior_low is None:
                hl_broken = True  # first break of current swing low treated as structure break

        if close is not None and prev_close is not None:
            if close < prev_close:
                consecutive_lower += 1
            else:
                consecutive_lower = 0

        crit = compute_bar_criteria(
            row.to_dict(),
            prev_close=prev_close,
            last_swing_high=last_high,
            last_swing_low=last_low,
            prior_swing_low=prior_low,
            consecutive_lower_closes=consecutive_lower,
            lower_high_confirmed=lower_high_confirmed,
            hl_broken=hl_broken,
            swing_low_broken=swing_low_broken,
            cfg=cfg,
        )
        rt, meta = step_state(rt, crit, cfg)

        if start_ts is None or dts >= start_ts:
            rows.append(
                {
                    "decision_time": dts,
                    "candle_timestamp": row["timestamp"],
                    "variant": cfg.variant,
                    "candle_closed_available": True,
                    **crit,
                    **meta,
                    "active_criteria": (
                        crit["confirmed_active_criteria"]
                        if meta["state"] == "confirmed_bearish_trend"
                        else crit["early_active_criteria"]
                        if meta["state"] == "early_bearish_trend"
                        else crit["warning_active_criteria"]
                        if meta["state"] == "bearish_warning"
                        else []
                    ),
                    "config_block_on": cfg.block_on,
                }
            )

        if close is not None:
            prev_close = close

    return pd.DataFrame(rows)

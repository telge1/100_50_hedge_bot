"""Phase F0 forward outcomes, first-touch races, and hypothetical recovery attempts.

Recovery PnL accounting
-----------------------
A 25%% short reduce creates a 25%% net-long exposure. Incremental edge vs full
lock is ONLY the net-long price move between short-close and short-reopen,
minus fees and slippage:

    gross = unlock_qty * (relock_fill - unlock_fill)   # long-like
    fees  = fee(unlock) + fee(relock)
    net   = gross - fees

Realized short PnL vs short_avg at unlock is intentionally excluded — that
profit existed under full lock as unrealized short PnL.
"""

from __future__ import annotations

from typing import Any, Sequence

from .cost_model import (
    apply_long_open_slippage,
    apply_short_open_slippage,
    fee_usdt,
    informative_slippage_cost_usdt,
)
from .phase_f0_speed import PhaseF0Config, _ts_iso, BAR_SECONDS_5M


def _qty_from_notional(notional: float, price: float) -> float:
    return float(notional) / float(price)


def forward_outcomes_from_bar(
    candles: Sequence[dict[str, Any]],
    *,
    entry_bar: int,
    entry_price: float,
    horizons: Sequence[int],
    event_id: str = "",
    level_pct: float | None = None,
) -> list[dict[str, Any]]:
    """MFE/MAE and time-to-thresholds after ``entry_bar`` (exclusive of past)."""
    rows: list[dict[str, Any]] = []
    if entry_bar < 0 or entry_bar >= len(candles) or entry_price <= 0:
        return rows
    thresholds_up = (0.0025, 0.005, 0.0075, 0.01, 0.015, 0.02)
    thresholds_dn = (0.0025, 0.005, 0.0075, 0.01, 0.015, 0.02)

    # Precompute first-hit bars after entry.
    first_up: dict[float, int | None] = {t: None for t in thresholds_up}
    first_dn: dict[float, int | None] = {t: None for t in thresholds_dn}
    max_high = float(candles[entry_bar]["high"])
    min_low = float(candles[entry_bar]["low"])
    for j in range(entry_bar + 1, len(candles)):
        h = float(candles[j]["high"])
        l = float(candles[j]["low"])
        max_high = max(max_high, h)
        min_low = min(min_low, l)
        up = (h - entry_price) / entry_price
        dn = (entry_price - l) / entry_price
        for t in thresholds_up:
            if first_up[t] is None and up + 1e-15 >= t:
                first_up[t] = j
        for t in thresholds_dn:
            if first_dn[t] is None and dn + 1e-15 >= t:
                first_dn[t] = j

    for h_bars in horizons:
        end = entry_bar + int(h_bars)
        complete = end < len(candles)
        last = min(end, len(candles) - 1)
        if last <= entry_bar:
            rows.append(
                {
                    "event_id": event_id,
                    "level_pct": level_pct,
                    "entry_bar": entry_bar,
                    "entry_price": entry_price,
                    "horizon_bars": h_bars,
                    "horizon_complete": False,
                    "max_rebound_pct": None,
                    "max_further_drop_pct": None,
                    "mfe_pct": None,
                    "mae_pct": None,
                    "close_return_pct": None,
                    "high_return_pct": None,
                    "low_return_pct": None,
                }
            )
            continue
        hi = max(float(candles[j]["high"]) for j in range(entry_bar, last + 1))
        lo = min(float(candles[j]["low"]) for j in range(entry_bar, last + 1))
        close = float(candles[last]["close"])
        rebound = (hi - entry_price) / entry_price
        drop = (entry_price - lo) / entry_price
        row: dict[str, Any] = {
            "event_id": event_id,
            "level_pct": level_pct,
            "entry_bar": entry_bar,
            "entry_price": entry_price,
            "entry_timestamp": _ts_iso(candles[entry_bar]["timestamp"]),
            "horizon_bars": h_bars,
            "horizon_complete": complete,
            "max_rebound_pct": rebound,
            "max_further_drop_pct": drop,
            "mfe_pct": rebound,  # long-release MFE
            "mae_pct": drop,  # long-release MAE
            "close_return_pct": (close - entry_price) / entry_price,
            "high_return_pct": rebound,
            "low_return_pct": -drop,
        }
        for t in thresholds_up:
            hit = first_up[t]
            key = f"bars_to_plus_{int(t * 10000):04d}"
            if hit is None:
                row[key] = None
                row[f"reached_plus_{int(t * 10000):04d}"] = False
            elif hit <= last:
                row[key] = hit - entry_bar
                row[f"reached_plus_{int(t * 10000):04d}"] = True
            else:
                row[key] = None
                row[f"reached_plus_{int(t * 10000):04d}"] = False if complete else None
        for t in thresholds_dn:
            hit = first_dn[t]
            key = f"bars_to_minus_{int(t * 10000):04d}"
            if hit is None:
                row[key] = None
                row[f"reached_minus_{int(t * 10000):04d}"] = False
            elif hit <= last:
                row[key] = hit - entry_bar
                row[f"reached_minus_{int(t * 10000):04d}"] = True
            else:
                row[key] = None
                row[f"reached_minus_{int(t * 10000):04d}"] = False if complete else None
        rows.append(row)
    return rows


def first_touch_race(
    candles: Sequence[dict[str, Any]],
    *,
    entry_bar: int,
    entry_price: float,
    tp_pct: float,
    stop_pct: float,
    same_bar_policy: str = "stop_first",
    event_id: str = "",
    level_pct: float | None = None,
) -> dict[str, Any]:
    """First-touch competition between TP and stop after entry_bar."""
    tp_level = entry_price * (1.0 + float(tp_pct))
    stop_level = entry_price * (1.0 - float(stop_pct))
    result: dict[str, Any] = {
        "event_id": event_id,
        "level_pct": level_pct,
        "entry_bar": entry_bar,
        "entry_price": entry_price,
        "tp_pct": tp_pct,
        "stop_pct": stop_pct,
        "tp_level": tp_level,
        "stop_level": stop_level,
        "winner": "neither",
        "winner_bar": None,
        "winner_timestamp": None,
        "bars_to_touch": None,
        "fill_price": None,
        "same_bar_collision": False,
        "window_incomplete": True,
        "same_bar_policy": same_bar_policy,
    }
    for j in range(entry_bar + 1, len(candles)):
        h = float(candles[j]["high"])
        l = float(candles[j]["low"])
        hit_tp = h >= tp_level - 1e-15
        hit_stop = l <= stop_level + 1e-15
        if hit_tp and hit_stop:
            result["same_bar_collision"] = True
            result["window_incomplete"] = False
            result["winner_bar"] = j
            result["winner_timestamp"] = _ts_iso(candles[j]["timestamp"])
            result["bars_to_touch"] = j - entry_bar
            if same_bar_policy == "stop_first":
                result["winner"] = "stop"
                result["fill_price"] = stop_level
            else:
                result["winner"] = "tp"
                result["fill_price"] = tp_level
            return result
        if hit_stop:
            result["winner"] = "stop"
            result["fill_price"] = stop_level
            result["winner_bar"] = j
            result["winner_timestamp"] = _ts_iso(candles[j]["timestamp"])
            result["bars_to_touch"] = j - entry_bar
            result["window_incomplete"] = False
            return result
        if hit_tp:
            result["winner"] = "tp"
            result["fill_price"] = tp_level
            result["winner_bar"] = j
            result["winner_timestamp"] = _ts_iso(candles[j]["timestamp"])
            result["bars_to_touch"] = j - entry_bar
            result["window_incomplete"] = False
            return result
    return result


def _recovery_attempt_pnl(
    *,
    unlock_fill: float,
    relock_fill: float,
    unlock_qty: float,
    fee_rate: float,
    slippage_bps: float,
) -> dict[str, float]:
    """Net-long incremental PnL between unlock and relock fills."""
    gross = float(unlock_qty) * (float(relock_fill) - float(unlock_fill))
    close_fee = fee_usdt(fill_price=unlock_fill, qty=unlock_qty, fee_rate=fee_rate)
    reopen_fee = fee_usdt(fill_price=relock_fill, qty=unlock_qty, fee_rate=fee_rate)
    # Informative slippage already in fills; report separately.
    slip_close = informative_slippage_cost_usdt(
        side="long",
        reference_price=unlock_fill / (1.0 + float(slippage_bps) / 10_000.0)
        if slippage_bps
        else unlock_fill,
        fill_price=unlock_fill,
        qty=unlock_qty,
    )
    slip_open = informative_slippage_cost_usdt(
        side="short",
        reference_price=relock_fill / (1.0 - float(slippage_bps) / 10_000.0)
        if slippage_bps
        else relock_fill,
        fill_price=relock_fill,
        qty=unlock_qty,
    )
    fees = close_fee + reopen_fee
    net = gross - fees
    return {
        "gross_directional_pnl": gross,
        "short_close_fee": close_fee,
        "short_reopen_fee": reopen_fee,
        "fee_cost": fees,
        "slippage_cost": float(slip_close + slip_open),
        "net_attempt_pnl": net,
    }


def simulate_recovery_attempt(
    candles: Sequence[dict[str, Any]],
    *,
    entry_bar: int,
    entry_ref_price: float,
    cfg: PhaseF0Config,
    unlock_qty: float,
    variant: str,
    event_id: str = "",
    level_pct: float | None = None,
    filter_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Single-attempt 25%% unlock with TP/stop race; returns PnL vs full lock edge."""
    # Conservative unlock fill: buy-to-close at adverse slippage vs level.
    unlock_fill = apply_long_open_slippage(
        reference_price=float(entry_ref_price), slippage_bps=cfg.slippage_bps
    )
    tp_pct = float(cfg.recovery_tp_pct)
    stop_pct = float(cfg.recovery_stop_pct)
    race = first_touch_race(
        candles,
        entry_bar=entry_bar,
        entry_price=unlock_fill,
        tp_pct=tp_pct,
        stop_pct=stop_pct,
        same_bar_policy=cfg.same_bar_collision_policy,
        event_id=event_id,
        level_pct=level_pct,
    )
    base: dict[str, Any] = {
        "event_id": event_id,
        "variant": variant,
        "level_pct": level_pct,
        "entry_bar": entry_bar,
        "entry_timestamp": _ts_iso(candles[entry_bar]["timestamp"]),
        "entry_ref_price": entry_ref_price,
        "unlock_fill": unlock_fill,
        "unlock_qty": unlock_qty,
        "unlock_fraction": cfg.test_unlock_fraction,
        "tp_pct": tp_pct,
        "stop_pct": stop_pct,
        "started": True,
        "completed": False,
        "winner": race["winner"],
        "bars_to_exit": race["bars_to_touch"],
        "exit_bar": race["winner_bar"],
        "exit_timestamp": race["winner_timestamp"],
        "same_bar_collision": race["same_bar_collision"],
        "window_incomplete": race["window_incomplete"],
        "gross_directional_pnl": None,
        "fee_cost": None,
        "slippage_cost": None,
        "net_attempt_pnl": None,
        "incremental_pnl_vs_full_lock": None,
        "max_added_loss_vs_full_lock": None,
        **(filter_meta or {}),
    }
    if race["winner"] not in {"tp", "stop"} or race["fill_price"] is None:
        return base

    # Relock fill: re-open short with adverse slippage vs exit reference.
    exit_ref = float(race["fill_price"])
    relock_fill = apply_short_open_slippage(
        reference_price=exit_ref, slippage_bps=cfg.slippage_bps
    )
    pnl = _recovery_attempt_pnl(
        unlock_fill=unlock_fill,
        relock_fill=relock_fill,
        unlock_qty=unlock_qty,
        fee_rate=cfg.fee_rate,
        slippage_bps=cfg.slippage_bps,
    )
    # Max adverse while open (long MAE on unlock_fill).
    exit_bar = int(race["winner_bar"])
    lo = min(
        float(candles[j]["low"]) for j in range(entry_bar, exit_bar + 1)
    )
    mae = max(unlock_fill - lo, 0.0) * float(unlock_qty)
    # Approximate added loss vs staying locked: adverse net-long move + fees.
    added = max(mae + pnl["fee_cost"] - max(pnl["gross_directional_pnl"], 0.0), 0.0)
    if race["winner"] == "stop":
        added = max(-pnl["net_attempt_pnl"], 0.0)

    base.update(
        {
            "completed": True,
            "relock_fill": relock_fill,
            "exit_ref_price": exit_ref,
            **pnl,
            "incremental_pnl_vs_full_lock": pnl["net_attempt_pnl"],
            "max_added_loss_vs_full_lock": added,
        }
    )
    return base


def find_rebound_entry_bar(
    candles: Sequence[dict[str, Any]],
    *,
    start_bar: int,
    rebound_pct: float,
) -> tuple[int | None, float | None]:
    """Causal: track running low from start_bar; entry when high >= low*(1+pct)."""
    if start_bar >= len(candles):
        return None, None
    running_low = float(candles[start_bar]["low"])
    for j in range(start_bar, len(candles)):
        low = float(candles[j]["low"])
        high = float(candles[j]["high"])
        if low < running_low:
            running_low = low
        if running_low > 0 and high + 1e-15 >= running_low * (1.0 + rebound_pct):
            return j, running_low * (1.0 + rebound_pct)
    return None, None


def find_reclaim_close_bar(
    candles: Sequence[dict[str, Any]],
    *,
    start_bar: int,
    reclaim_price: float,
) -> int | None:
    """First close strictly above reclaim_price after start_bar."""
    for j in range(start_bar + 1, len(candles)):
        if float(candles[j]["close"]) > float(reclaim_price) + 1e-15:
            return j
    return None


def build_recovery_attempts_for_crossing(
    candles: Sequence[dict[str, Any]],
    crossing: dict[str, Any],
    leg: dict[str, Any] | None,
    prev_level_price: float | None,
    cfg: PhaseF0Config,
    *,
    unlock_qty: float,
) -> list[dict[str, Any]]:
    """Generate R0–R5 diagnostic attempts for one level crossing."""
    entry_bar = int(crossing["end_bar"])
    level_price = float(crossing["level_price"])
    level_pct = float(crossing["level_pct"])
    event_id = str(crossing.get("event_id") or "")
    attempts: list[dict[str, Any]] = []

    # R0
    attempts.append(
        simulate_recovery_attempt(
            candles,
            entry_bar=entry_bar,
            entry_ref_price=level_price,
            cfg=cfg,
            unlock_qty=unlock_qty,
            variant="R0_unfiltered",
            event_id=event_id,
            level_pct=level_pct,
        )
    )

    # R1 wait bars
    for wb in cfg.wait_bars:
        start = entry_bar + int(wb)
        if start >= len(candles):
            attempts.append(
                {
                    "event_id": event_id,
                    "variant": f"R1_wait_{wb}",
                    "level_pct": level_pct,
                    "entry_bar": entry_bar,
                    "started": False,
                    "completed": False,
                    "skip_reason": "insufficient_data_after_wait",
                    "wait_bars": wb,
                }
            )
            continue
        attempts.append(
            simulate_recovery_attempt(
                candles,
                entry_bar=start,
                entry_ref_price=float(candles[start]["close"]),
                cfg=cfg,
                unlock_qty=unlock_qty,
                variant=f"R1_wait_{wb}",
                event_id=event_id,
                level_pct=level_pct,
                filter_meta={"wait_bars": wb},
            )
        )

    # R2 rebound confirmation
    reb_bar, reb_px = find_rebound_entry_bar(
        candles, start_bar=entry_bar, rebound_pct=cfg.rebound_confirm_pct
    )
    if reb_bar is None:
        attempts.append(
            {
                "event_id": event_id,
                "variant": "R2_rebound_confirm",
                "level_pct": level_pct,
                "entry_bar": entry_bar,
                "started": False,
                "completed": False,
                "skip_reason": "no_rebound_confirm",
            }
        )
    else:
        attempts.append(
            simulate_recovery_attempt(
                candles,
                entry_bar=reb_bar,
                entry_ref_price=float(reb_px or candles[reb_bar]["close"]),
                cfg=cfg,
                unlock_qty=unlock_qty,
                variant="R2_rebound_confirm",
                event_id=event_id,
                level_pct=level_pct,
            )
        )

    # R3 reclaim previous level
    if prev_level_price is None:
        attempts.append(
            {
                "event_id": event_id,
                "variant": "R3_reclaim_prev_level",
                "level_pct": level_pct,
                "entry_bar": entry_bar,
                "started": False,
                "completed": False,
                "skip_reason": "no_previous_level",
            }
        )
    else:
        reclaim_bar = find_reclaim_close_bar(
            candles, start_bar=entry_bar, reclaim_price=prev_level_price
        )
        if reclaim_bar is None:
            attempts.append(
                {
                    "event_id": event_id,
                    "variant": "R3_reclaim_prev_level",
                    "level_pct": level_pct,
                    "entry_bar": entry_bar,
                    "started": False,
                    "completed": False,
                    "skip_reason": "no_reclaim",
                }
            )
        else:
            attempts.append(
                simulate_recovery_attempt(
                    candles,
                    entry_bar=reclaim_bar,
                    entry_ref_price=prev_level_price,
                    cfg=cfg,
                    unlock_qty=unlock_qty,
                    variant="R3_reclaim_prev_level",
                    event_id=event_id,
                    level_pct=level_pct,
                )
            )

    # R4 speed filter buckets on prior completed leg ending at this level
    slowdown = None if leg is None else leg.get("slowdown_ratio")
    bucket = None if leg is None else leg.get("slowdown_bucket")
    cpe = None if leg is None else leg.get("close_path_efficiency")
    n_reb05 = 0 if leg is None else int(leg.get("number_of_rebounds_0_50pct") or 0)

    r4_specs = [
        ("R4_bucket_<0.50_stark_beschleunigt", bucket == "<0.50_stark_beschleunigt"),
        ("R4_bucket_0.50-0.80_beschleunigt", bucket == "0.50-0.80_beschleunigt"),
        ("R4_bucket_0.80-1.25_aehnlich", bucket == "0.80-1.25_aehnlich"),
        ("R4_bucket_1.25-2.00_verlangsamt", bucket == "1.25-2.00_verlangsamt"),
        ("R4_bucket_>2.00_stark_verlangsamt", bucket == ">2.00_stark_verlangsamt"),
        (
            "R4_slowdown_ge_1.25",
            slowdown is not None
            and slowdown == slowdown
            and float(slowdown) >= 1.25,
        ),
        (
            "R4_slowdown_ge_2.00",
            slowdown is not None
            and slowdown == slowdown
            and float(slowdown) >= 2.00,
        ),
    ]
    for name, ok in r4_specs:
        if not ok:
            attempts.append(
                {
                    "event_id": event_id,
                    "variant": name,
                    "level_pct": level_pct,
                    "entry_bar": entry_bar,
                    "started": False,
                    "completed": False,
                    "skip_reason": "speed_filter_not_met",
                    "slowdown_ratio": slowdown,
                    "slowdown_bucket": bucket,
                }
            )
            continue
        attempts.append(
            simulate_recovery_attempt(
                candles,
                entry_bar=entry_bar,
                entry_ref_price=level_price,
                cfg=cfg,
                unlock_qty=unlock_qty,
                variant=name,
                event_id=event_id,
                level_pct=level_pct,
                filter_meta={"slowdown_ratio": slowdown, "slowdown_bucket": bucket},
            )
        )

    # R5 combinations
    r5_specs = [
        (
            "R5_slowdown_ge_1.25_and_cpe_lt_0.35",
            slowdown is not None
            and slowdown == slowdown
            and float(slowdown) >= 1.25
            and cpe is not None
            and float(cpe) < 0.35,
        ),
        (
            "R5_slowdown_ge_1.25_and_cpe_ge_0.35",
            slowdown is not None
            and slowdown == slowdown
            and float(slowdown) >= 1.25
            and cpe is not None
            and float(cpe) >= 0.35,
        ),
        (
            "R5_slowdown_ge_2.00_and_reb050",
            slowdown is not None
            and slowdown == slowdown
            and float(slowdown) >= 2.00
            and n_reb05 >= 1,
        ),
    ]
    for name, ok in r5_specs:
        if not ok:
            attempts.append(
                {
                    "event_id": event_id,
                    "variant": name,
                    "level_pct": level_pct,
                    "entry_bar": entry_bar,
                    "started": False,
                    "completed": False,
                    "skip_reason": "combo_filter_not_met",
                    "slowdown_ratio": slowdown,
                    "close_path_efficiency": cpe,
                }
            )
            continue
        attempts.append(
            simulate_recovery_attempt(
                candles,
                entry_bar=entry_bar,
                entry_ref_price=level_price,
                cfg=cfg,
                unlock_qty=unlock_qty,
                variant=name,
                event_id=event_id,
                level_pct=level_pct,
                filter_meta={
                    "slowdown_ratio": slowdown,
                    "close_path_efficiency": cpe,
                    "number_of_rebounds_0_50pct": n_reb05,
                },
            )
        )

    _ = BAR_SECONDS_5M  # documented 5m semantics
    return attempts

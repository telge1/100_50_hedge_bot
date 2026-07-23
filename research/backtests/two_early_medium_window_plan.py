"""Deterministic coin universe + chronological window planning for large TEM validation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from research.backtests.candle_loader import load_candles_for_symbol
from research.backtests.historical_backtest import normalize_candles
from research.backtests.inventory_mtm_freeze import safe_float
from research.backtests.multicoin_blocker_price_staging import DEFAULT_BASELINE
from research.backtests.recovery_reentry_policy import load_baseline_blockers
from research.backtests.run_inventory_mtm_neg1_policy_audit import load_baseline_coin_list
from research.backtests.two_early_medium_multistart_starts import (
    DEFAULT_GRID_STEP,
    DEFAULT_MIN_REMAINING,
    DEFAULT_SEED,
    DEFAULT_WARMUP,
    StartPoint,
    select_start_points_for_coin,
)

MIN_CANDLES_FOR_INCLUSION = 20_000
MIN_WINDOW_STARTS = 15
TARGET_STARTS_PER_WINDOW = 25
MIN_REMAINING_IN_WINDOW = 800
RECENT_WINDOW_BARS = 12_000
FULL_HISTORY_TARGET_STARTS = 30


@dataclass(frozen=True)
class TimeWindow:
    window_id: str
    coin: str
    start_index_lo: int  # inclusive — starts must be >=
    start_index_hi: int  # inclusive — starts must be <=
    run_end_index: int  # exclusive — backtest truncates here (candles[:run_end_index])
    start_ts: str | None
    end_ts: str | None
    n_candles_in_window: int
    kind: str  # early|middle|late|recent|full_history


def window_pair_key(coin: str, window_id: str, start_index: int) -> str:
    return f"{str(coin).upper()}|{window_id}|{int(start_index)}"


def window_profile_run_key(coin: str, window_id: str, start_index: int, profile: str) -> str:
    return f"{window_pair_key(coin, window_id, start_index)}|{profile}"


def _ts(c: Any) -> str | None:
    if c is None:
        return None
    raw = c.get("timestamp") if isinstance(c, dict) else getattr(c, "timestamp", None)
    if raw is None:
        return None
    try:
        t = raw if hasattr(raw, "isoformat") else datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if getattr(t, "tzinfo", None) is None:
            t = t.replace(tzinfo=timezone.utc)
        return str(t)
    except Exception:  # noqa: BLE001
        return str(raw)


def discover_coin_universe(
    *,
    min_candles: int = MIN_CANDLES_FOR_INCLUSION,
    candle_limit: int | None = None,
    extra_required: Sequence[str] = (),
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Deterministic universe from baseline coin_manifest + required priors.

    Returns (included_rows, excluded_rows). No PnL-based filtering.
    """
    try:
        ordered = load_baseline_coin_list()
    except FileNotFoundError:
        blockers = load_baseline_blockers(DEFAULT_BASELINE / "blocker_trades.csv")
        ordered = sorted({str(r.get("coin") or "").upper() for r in blockers if r.get("coin")})

    # Ensure prior multistart / regression coins are considered
    required = [
        c.upper()
        for c in (
            "APTUSDT",
            "ATOMUSDT",
            "TRXUSDT",
            "ADAUSDT",
            "ARBUSDT",
            "BTCUSDT",
            "ETHUSDT",
            *extra_required,
        )
    ]
    candidates = list(dict.fromkeys([*ordered, *required]))

    included: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for coin in candidates:
        try:
            raw = load_candles_for_symbol(coin, limit=candle_limit)
            candles = normalize_candles(coin, raw)
            n = len(candles)
            if n < int(min_candles):
                excluded.append(
                    {
                        "coin": coin,
                        "exclusion_reason": "insufficient_history",
                        "n_candles": n,
                        "min_required": min_candles,
                        "missing_data": False,
                        "invalid_instrument_rules": False,
                        "notes": f"have {n} candles",
                    }
                )
                continue
            # Light instrument sanity: first/last timestamps + OHLC finite
            first = candles[0]
            last = candles[-1]
            for field in ("open", "high", "low", "close"):
                v = float(first[field] if isinstance(first, dict) else getattr(first, field))
                if v <= 0:
                    raise ValueError(f"non-positive {field}")
            included.append(
                {
                    "coin": coin,
                    "n_candles": n,
                    "first_timestamp": _ts(first),
                    "last_timestamp": _ts(last),
                    "source": "baseline_manifest_or_required",
                    "included": True,
                }
            )
        except FileNotFoundError as exc:
            excluded.append(
                {
                    "coin": coin,
                    "exclusion_reason": "missing_data",
                    "n_candles": 0,
                    "min_required": min_candles,
                    "missing_data": True,
                    "invalid_instrument_rules": False,
                    "notes": str(exc),
                }
            )
        except Exception as exc:  # noqa: BLE001
            msg = str(exc).lower()
            excluded.append(
                {
                    "coin": coin,
                    "exclusion_reason": (
                        "invalid_instrument_rules"
                        if "instrument" in msg or "tick" in msg
                        else "technical_error"
                    ),
                    "n_candles": 0,
                    "min_required": min_candles,
                    "missing_data": False,
                    "invalid_instrument_rules": "instrument" in msg or "tick" in msg,
                    "notes": str(exc)[:300],
                }
            )
    return included, excluded


def build_time_windows_for_coin(
    coin: str,
    candles: Sequence[Any],
    *,
    warmup: int = DEFAULT_WARMUP,
    min_remaining_in_window: int = MIN_REMAINING_IN_WINDOW,
    recent_bars: int = RECENT_WINDOW_BARS,
) -> list[TimeWindow]:
    """Split history into early/middle/late (+ recent if long enough) + full_history.

    Boundaries are fixed from candle counts only — no outcome look-ahead.
    """
    n = len(candles)
    if n <= warmup + min_remaining_in_window + 10:
        return []

    # Usable start range for any start: [warmup, n - min_remaining_global)
    # For windowed runs, remaining is measured vs run_end, not series end.
    usable_lo = int(warmup)
    usable_hi = n - 1  # last candle index
    # Three chronological thirds of the usable span for *run segments*
    # Segment i covers candle indices [seg_lo, seg_hi) as run_end boundary.
    span = usable_hi - usable_lo + 1
    third = max(span // 3, min_remaining_in_window + 50)
    boundaries = [
        usable_lo,
        min(usable_lo + third, usable_hi + 1),
        min(usable_lo + 2 * third, usable_hi + 1),
        usable_hi + 1,
    ]
    # Ensure strictly increasing
    for i in range(1, len(boundaries)):
        if boundaries[i] <= boundaries[i - 1]:
            boundaries[i] = min(boundaries[i - 1] + min_remaining_in_window + 20, usable_hi + 1)

    kinds = ("early", "middle", "late")
    windows: list[TimeWindow] = []
    for kind, seg_lo, seg_hi in zip(kinds, boundaries[:-1], boundaries[1:]):
        run_end = int(seg_hi)
        # Starts must leave min_remaining_in_window before run_end
        start_hi = run_end - int(min_remaining_in_window) - 1
        start_lo = int(seg_lo)
        if start_hi < start_lo:
            continue
        windows.append(
            TimeWindow(
                window_id=f"{kind}",
                coin=coin.upper(),
                start_index_lo=start_lo,
                start_index_hi=start_hi,
                run_end_index=run_end,
                start_ts=_ts(candles[start_lo]) if start_lo < n else None,
                end_ts=_ts(candles[run_end - 1]) if run_end - 1 < n else None,
                n_candles_in_window=run_end - start_lo,
                kind=kind,
            )
        )

    # Recent window: last recent_bars of series
    if n >= warmup + recent_bars + min_remaining_in_window:
        run_end = n
        start_lo = max(usable_lo, n - recent_bars)
        start_hi = run_end - min_remaining_in_window - 1
        if start_hi >= start_lo:
            windows.append(
                TimeWindow(
                    window_id="recent",
                    coin=coin.upper(),
                    start_index_lo=start_lo,
                    start_index_hi=start_hi,
                    run_end_index=run_end,
                    start_ts=_ts(candles[start_lo]),
                    end_ts=_ts(candles[n - 1]),
                    n_candles_in_window=run_end - start_lo,
                    kind="recent",
                )
            )

    # Full-history reference: starts across full usable, run to series end
    full_hi = n - min_remaining_in_window - 1
    if full_hi >= usable_lo:
        windows.append(
            TimeWindow(
                window_id="full_history",
                coin=coin.upper(),
                start_index_lo=usable_lo,
                start_index_hi=full_hi,
                run_end_index=n,
                start_ts=_ts(candles[usable_lo]),
                end_ts=_ts(candles[n - 1]),
                n_candles_in_window=n - usable_lo,
                kind="full_history",
            )
        )
    return windows


def select_starts_for_window(
    *,
    coin: str,
    candles: Sequence[Any],
    window: TimeWindow,
    blocker_starts: Sequence[int],
    target_starts: int = TARGET_STARTS_PER_WINDOW,
    seed: int = DEFAULT_SEED,
    warmup: int = DEFAULT_WARMUP,
    grid_step: int | None = None,
    smoke: bool = False,
) -> list[dict[str, Any]]:
    """Causal start selection restricted to a window; identical for both profiles."""
    # Slice candles for selection features: full history up to run_end (causal).
    # Regime at start_index only uses 0..start_index via select_start_points_for_coin.
    series = list(candles[: window.run_end_index])
    # Temporarily constrain eligibility by wrapping select with filtered historical blockers
    # and a reduced target; then filter to window bounds.
    step = grid_step
    if step is None:
        span = max(window.start_index_hi - window.start_index_lo, 1)
        step = max(80, span // max(target_starts, 1))
    if smoke:
        target = min(4, target_starts)
        step = max(step, 500)
        regime_quota, random_quota, grid_quota = 1, 1, 1
    else:
        target = int(target_starts)
        regime_quota, random_quota, grid_quota = 3, 4, max(6, target // 3)

    blockers_in_window = [
        int(i)
        for i in blocker_starts
        if window.start_index_lo <= int(i) <= window.start_index_hi
    ]
    # min_remaining relative to sliced series length
    min_rem = max(50, window.run_end_index - window.start_index_hi - 1)
    pts = select_start_points_for_coin(
        coin=coin,
        candles=series,
        historical_blocker_starts=blockers_in_window,
        target_total=max(target, MIN_WINDOW_STARTS if not smoke else 2),
        seed=seed + int(hashlib_mod(coin, window.window_id)),
        warmup=max(warmup, window.start_index_lo),  # do not start before window/warmup
        min_remaining=min_rem,
        grid_step=int(step),
        regime_quota=regime_quota,
        random_quota=random_quota,
        grid_quota=grid_quota,
    )
    # Hard filter to window start bounds + dedupe
    seen: set[int] = set()
    rows: list[dict[str, Any]] = []
    for p in pts:
        idx = int(p.start_index)
        if idx < window.start_index_lo or idx > window.start_index_hi:
            continue
        if idx in seen:
            continue
        seen.add(idx)
        max_window_candles = int(window.run_end_index - idx)
        if max_window_candles < 50:
            continue
        rows.append(
            {
                "coin": coin.upper(),
                "window_id": window.window_id,
                "window_kind": window.kind,
                "start_index": idx,
                "run_end_index": window.run_end_index,
                "max_window_candles": max_window_candles,
                "pair_key": window_pair_key(coin, window.window_id, idx),
                "primary_category": p.primary_category,
                "categories": list(p.categories),
                "selection_rank": p.selection_rank,
                "is_historical_blocker": "historical_blocker" in p.categories,
                "is_neutral_pool": "neutral_pool" in p.categories,
                "window_start_ts": window.start_ts,
                "window_end_ts": window.end_ts,
                "start_ts": _ts(candles[idx]) if idx < len(candles) else None,
                **{f"feat_{k}": v for k, v in (p.causal_features or {}).items()},
            }
        )
    # Cap to target while keeping blockers
    if len(rows) > target:
        blockers = [r for r in rows if r["is_historical_blocker"]]
        others = [r for r in rows if not r["is_historical_blocker"]]
        keep_n = max(0, target - len(blockers))
        rows = blockers + others[:keep_n]
        rows = sorted(rows, key=lambda r: int(r["start_index"]))
    return rows


def hashlib_mod(coin: str, window_id: str) -> int:
    import hashlib

    digest = hashlib.sha256(f"{coin}|{window_id}".encode()).hexdigest()
    return int(digest[:8], 16) % 10_000


def windows_to_rows(windows: Sequence[TimeWindow]) -> list[dict[str, Any]]:
    return [
        {
            "coin": w.coin,
            "window_id": w.window_id,
            "window_kind": w.kind,
            "start_index_lo": w.start_index_lo,
            "start_index_hi": w.start_index_hi,
            "run_end_index": w.run_end_index,
            "start_ts": w.start_ts,
            "end_ts": w.end_ts,
            "n_candles_in_window": w.n_candles_in_window,
        }
        for w in windows
    ]

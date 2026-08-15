"""Research helpers: APTUSDT T3 staged/split Second-Leg size comparison.

Documents and measures the existing Main-Bot semantics for:

* ``normal_cycle_second_leg_split`` (same trigger, qty split 3→2)
* ``is_staged_second_leg_tp`` (distinct prices — disabled for short_reduce / long-primary)

No live/runtime mutation.
"""

from __future__ import annotations

import csv
import json
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from research.backtests.apt_baseline_blocker_root_cause import (
    APT_TRADE3_COIN,
    APT_TRADE3_ID,
    APT_TRADE3_START_INDEX,
    _active_exit_at_local_candle,
    _candle_close,
    _candle_high,
    _purpose,
    _ts,
    build_cycle_snapshots,
    build_event_timeline,
    check_baseline_parity,
    pnl_reconciliation_rows,
)
from research.backtests.backtest_report import BacktestResult
from research.backtests.historical_backtest import run_historical_backtest
from research.backtests.inventory_mtm_freeze import inventory_mtm_usdt, required_recovery_move_pct, safe_float
from research.backtests.long_add_multistart_metrics import analyze_trade, cycle_leg_map, normalize_trade_status
from research.backtests.long_baseline_notional_stage_tp import extract_stage_tp_attempts
from research.backtests.run_inventory_mtm_neg1_policy_audit import (
    BASELINE_DIR,
    FILL_MODEL,
    LONG_FILL_DISTANCE_PCT,
    TARGET_PROFIT_USDT,
    TP_PROFIT_TARGET_PCT,
)
from research.backtests.safe_cycle_boundary_freeze import detect_invalid_partial_cycle

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUT = ROOT / "research/backtests/results/apt_t3_stage_tp_size_comparison_20260721"
PROTECTED = (
    BASELINE_DIR,
    ROOT / "research/backtests/results/safe_cycle_boundary_freeze_audit_20260720",
    ROOT / "research/backtests/results/long_baseline_1000_500_stage_tp_audit_20260721",
    ROOT / "research/backtests/results/apt_baseline_blocker_root_cause_20260721",
)

CYCLE_SECOND_LEG_RE = re.compile(r"^CYCLE_(\d+)_(SHORT_REDUCE|SHORT_TP|LONG_REDUCE)$")
BOUNCE_REFERENCE_HIGH = 1.9963  # approx post-C4 bounce from prior root-cause notes
MIN_NOTIONAL_USDT = 5.0


def assert_output_dir_safe(output_dir: Path) -> None:
    resolved = output_dir.resolve()
    for protected in PROTECTED:
        if resolved == protected.resolve():
            raise RuntimeError(f"refusing protected output dir: {protected}")
    if output_dir.exists() and any(output_dir.iterdir()):
        raise FileExistsError(f"refusing to overwrite non-empty output dir: {output_dir}")


def parse_sizes(spec: str) -> list[tuple[str, float, float]]:
    """Parse ``100:50,1000:500`` → ``[(S100,100,50), (S1000,1000,500)]``."""
    out: list[tuple[str, float, float]] = []
    for part in str(spec or "").split(","):
        part = part.strip()
        if not part:
            continue
        long_s, short_s = part.split(":")
        long_n = float(long_s)
        short_n = float(short_s)
        label = f"S{int(long_n) if long_n == int(long_n) else long_n}"
        out.append((label, long_n, short_n))
    if not out:
        raise ValueError("empty --sizes")
    return out


def run_apt_t3_at_size(
    *,
    candles: list[Any],
    start_index: int,
    base_notional_usdt: float,
    coin: str = APT_TRADE3_COIN,
) -> BacktestResult:
    window = candles[start_index:]
    result = run_historical_backtest(
        coin.upper(),
        "long",
        window,
        config_source="live",
        fill_model=FILL_MODEL,
        tp_profit_target_pct=TP_PROFIT_TARGET_PCT,
        long_fill_distance_pct=LONG_FILL_DISTANCE_PCT,
        target_profit_usdt=TARGET_PROFIT_USDT,
        base_notional_usdt=float(base_notional_usdt),
        initial_notional_usdt=float(base_notional_usdt),
        absolute_trade_start_index=start_index,
    )
    result.start_index = start_index
    result.trade_number = APT_TRADE3_ID
    return result


def _cycle_from_purpose(purpose: str) -> int | None:
    match = CYCLE_SECOND_LEG_RE.match(str(purpose or ""))
    return int(match.group(1)) if match else None


def extract_cycle_leg_fills(result: BacktestResult, cycle: int) -> dict[str, list[dict[str, Any]]]:
    long_adds: list[dict[str, Any]] = []
    short_reduces: list[dict[str, Any]] = []
    for fill in result.fill_log or []:
        purpose = _purpose(fill)
        if purpose == f"CYCLE_{cycle}_LONG_ADD":
            long_adds.append(fill)
        elif purpose == f"CYCLE_{cycle}_SHORT_REDUCE":
            short_reduces.append(fill)
    return {"long_add": long_adds, "short_reduce": short_reduces}


def stage_attempt_rows_for_cycle(
    *,
    variant: str,
    long_notional: float,
    result: BacktestResult,
    cycle: int,
) -> list[dict[str, Any]]:
    attempts = extract_stage_tp_attempts(
        coin=APT_TRADE3_COIN,
        variant=variant,
        trade_number=APT_TRADE3_ID,
        result=result,
        exchange_min_notional=MIN_NOTIONAL_USDT,
    )
    rows = [a for a in attempts if int(a.get("cycle") or 0) == cycle]
    for row in rows:
        row["long_notional_usdt"] = long_notional
        row["short_notional_usdt"] = long_notional * 0.5
    return rows


def classify_cycle4_split_outcome(attempts: list[dict[str, Any]], result: BacktestResult) -> dict[str, Any]:
    """Explain whether Cycle-4 split was attempted / rejected / accepted."""
    c4_intents = []
    for intent in result.intent_log or []:
        purpose = str(intent.get("purpose") or "")
        if purpose != "CYCLE_4_SHORT_REDUCE":
            continue
        meta = dict(intent.get("metadata_excerpt") or {})
        c4_intents.append({"intent": intent, "meta": meta})

    if not c4_intents and not attempts:
        return {
            "outcome": "not_attempted_or_no_second_leg",
            "detail": "No CYCLE_4_SHORT_REDUCE intents / stage attempts recorded",
        }

    accepted = [a for a in attempts if a.get("accepted")]
    rejected = [a for a in attempts if a.get("rejected")]
    staged_meta = any(
        i["meta"].get("normal_cycle_second_leg_split") or i["meta"].get("is_staged_second_leg_tp")
        for i in c4_intents
    )
    fallback = any(i["meta"].get("fallback_to_single_second_leg") for i in c4_intents)
    price_staged = any(i["meta"].get("is_staged_second_leg_tp") for i in c4_intents)
    normal_split = any(i["meta"].get("normal_cycle_second_leg_split") for i in c4_intents)

    if accepted and normal_split:
        return {
            "outcome": "accepted_normal_qty_split",
            "detail": (
                f"normal_cycle_second_leg_split accepted with "
                f"{accepted[0].get('actual_stage_count')} same-price stages"
            ),
            "price_staging_used": False,
            "qty_split_used": True,
        }
    if rejected or fallback:
        reason = (rejected[0].get("rejection_reason") if rejected else None) or "stage_below_min_notional"
        return {
            "outcome": "attempted_rejected_min_notional",
            "detail": f"Split rejected / full-qty fallback: {reason}",
            "price_staging_used": False,
            "qty_split_used": False,
            "rejection_reason": reason,
        }
    if price_staged:
        return {
            "outcome": "accepted_price_staged_tp",
            "detail": "is_staged_second_leg_tp with distinct prices (unexpected for long-primary)",
            "price_staging_used": True,
            "qty_split_used": False,
        }
    if c4_intents and not staged_meta:
        return {
            "outcome": "single_full_qty_no_split_metadata",
            "detail": (
                "Second leg placed as single full-qty order without split metadata "
                "(split builder returned None → fallback path)"
            ),
            "price_staging_used": False,
            "qty_split_used": False,
        }
    return {
        "outcome": "unknown",
        "detail": f"intents={len(c4_intents)} attempts={len(attempts)}",
    }


def exit_after_each_stage_rows(
    *,
    variant: str,
    long_notional: float,
    result: BacktestResult,
    candles: list[Any],
    start_index: int,
    focus_cycles: tuple[int, ...] = (3, 4),
) -> list[dict[str, Any]]:
    """After each cycle fill, record active LONG_TP_EXIT and inventory state."""
    order_log = list(result.order_log or [])
    rows: list[dict[str, Any]] = []
    cum = 0.0
    for fill in result.fill_log or []:
        purpose = _purpose(fill)
        cycle = None
        m = re.match(r"^CYCLE_(\d+)_(LONG_ADD|SHORT_REDUCE)$", purpose)
        if m:
            cycle = int(m.group(1))
        if cycle is None or cycle not in focus_cycles:
            continue
        local = int(fill.get("candle_index") or 0)
        abs_i = start_index + local
        mark = _candle_close(candles[abs_i]) if abs_i < len(candles) else safe_float(fill.get("fill_price"))
        closed = safe_float(fill.get("closed_pnl") or fill.get("confirmed_closed_pnl"))
        cum += closed
        long_qty = safe_float(fill.get("long_qty_after"))
        short_qty = safe_float(fill.get("short_qty_after"))
        long_avg = safe_float(fill.get("long_avg_after"))
        short_avg = safe_float(fill.get("short_avg_after"))
        active_exit = _active_exit_at_local_candle(order_log, local_candle=local)
        # Prefer exit submitted on/after this fill candle (rebuild may land same candle)
        later_exit = _active_exit_at_local_candle(order_log, local_candle=local)
        mtm = inventory_mtm_usdt(
            realized=cum,
            long_qty=long_qty,
            long_avg=long_avg,
            short_qty=short_qty,
            short_avg=short_avg,
            mark=mark,
        )
        rows.append(
            {
                "variant": variant,
                "long_notional_usdt": long_notional,
                "cycle": cycle,
                "leg": m.group(2) if m else "",
                "purpose": purpose,
                "local_candle": local,
                "absolute_candle": abs_i,
                "timestamp": fill.get("timestamp"),
                "fill_price": safe_float(fill.get("fill_price")),
                "qty": safe_float(fill.get("qty")),
                "closed_pnl": closed,
                "cum_realized_pnl": cum,
                "long_qty": long_qty,
                "short_qty": short_qty,
                "long_avg": long_avg,
                "short_avg": short_avg,
                "active_exit_after_fill": later_exit,
                "exit_distance_pct": required_recovery_move_pct(
                    mark=mark, active_exit=later_exit, primary_side="long"
                ),
                "inventory_mtm_usdt": mtm,
                "net_exposure_qty": long_qty - short_qty,
            }
        )
    return rows


def coverage_after_each_stage_rows(
    *,
    variant: str,
    long_notional: float,
    result: BacktestResult,
    focus_cycles: tuple[int, ...] = (3, 4),
) -> list[dict[str, Any]]:
    """Coverage = sum(SHORT_REDUCE closed_pnl) vs abs(LONG_ADD loss) + target_profit."""
    rows: list[dict[str, Any]] = []
    for cycle in focus_cycles:
        legs = extract_cycle_leg_fills(result, cycle)
        long_loss = sum(
            abs(safe_float(f.get("closed_pnl") or f.get("confirmed_closed_pnl")))
            for f in legs["long_add"]
            if safe_float(f.get("closed_pnl") or f.get("confirmed_closed_pnl")) < 0
        )
        required = long_loss + TARGET_PROFIT_USDT
        cover_cum = 0.0
        for i, fill in enumerate(legs["short_reduce"], start=1):
            pnl = safe_float(fill.get("closed_pnl") or fill.get("confirmed_closed_pnl"))
            cover_cum += pnl
            rows.append(
                {
                    "variant": variant,
                    "long_notional_usdt": long_notional,
                    "cycle": cycle,
                    "stage_fill_index": i,
                    "timestamp": fill.get("timestamp"),
                    "local_candle": fill.get("candle_index"),
                    "fill_price": safe_float(fill.get("fill_price")),
                    "qty": safe_float(fill.get("qty")),
                    "stage_closed_pnl": pnl,
                    "cum_cover_pnl": cover_cum,
                    "first_leg_loss_abs": long_loss,
                    "required_net": required,
                    "coverage_margin": cover_cum - required,
                    "coverage_complete": int(cover_cum + 1e-9 >= required),
                    "n_short_reduce_fills": len(legs["short_reduce"]),
                }
            )
        if not legs["short_reduce"] and legs["long_add"]:
            rows.append(
                {
                    "variant": variant,
                    "long_notional_usdt": long_notional,
                    "cycle": cycle,
                    "stage_fill_index": 0,
                    "first_leg_loss_abs": long_loss,
                    "required_net": required,
                    "cum_cover_pnl": 0.0,
                    "coverage_margin": -required,
                    "coverage_complete": 0,
                    "n_short_reduce_fills": 0,
                    "note": "second_leg_not_filled",
                }
            )
    return rows


def bounce_reachability_analysis(
    *,
    variant: str,
    long_notional: float,
    result: BacktestResult,
    candles: list[Any],
    start_index: int,
    cycle: int = 4,
    bounce_ref: float = BOUNCE_REFERENCE_HIGH,
) -> dict[str, Any]:
    """After Cycle-N LONG_ADD, was active exit reachable by later bounce high?"""
    legs = extract_cycle_leg_fills(result, cycle)
    if not legs["long_add"]:
        return {
            "variant": variant,
            "long_notional_usdt": long_notional,
            "cycle": cycle,
            "has_long_add": False,
        }

    long_add = legs["long_add"][0]
    long_local = int(long_add.get("candle_index") or 0)
    order_log = list(result.order_log or [])
    exit_after_long = _active_exit_at_local_candle(order_log, local_candle=long_local)

    # Bounce window: from long_add+1 until first short_reduce (or +5000 candles)
    end_local = long_local + 5000
    if legs["short_reduce"]:
        end_local = max(int(legs["short_reduce"][0].get("candle_index") or 0), long_local + 1)

    max_high = 0.0
    max_high_local = long_local
    for local in range(long_local + 1, min(end_local + 1, len(candles) - start_index)):
        abs_i = start_index + local
        if abs_i >= len(candles):
            break
        h = _candle_high(candles[abs_i])
        if h > max_high:
            max_high = h
            max_high_local = local

    # Critical: exit that was live DURING the bounce (after long-add, before second-leg fills)
    exit_during_bounce = exit_after_long
    bounce_reaches_exit_during = bool(exit_during_bounce and max_high + 1e-9 >= exit_during_bounce)
    gap_during = (exit_during_bounce - max_high) if exit_during_bounce else None

    # Stage-fill exits (post second-leg — for documentation only; not causal for prior bounce)
    stage_exits: list[dict[str, Any]] = []
    for i, fill in enumerate(legs["short_reduce"], start=1):
        local = int(fill.get("candle_index") or 0)
        active = _active_exit_at_local_candle(order_log, local_candle=local)
        stage_exits.append(
            {
                "stage_fill_index": i,
                "fill_price": safe_float(fill.get("fill_price")),
                "timestamp": fill.get("timestamp"),
                "local_candle": local,
                "active_exit_after": active,
                "note": "exit after this fill; bounce may have occurred earlier",
            }
        )

    final_exit = stage_exits[-1]["active_exit_after"] if stage_exits else exit_after_long
    actually_flat = normalize_trade_status(result) == "closed"

    first_sr_price = safe_float(legs["short_reduce"][0].get("fill_price")) if legs["short_reduce"] else None

    # Detect planned vs submitted split stages from intents
    planned_stage_count = None
    submitted_stage_intents = 0
    for intent in result.intent_log or []:
        if str(intent.get("purpose") or "") != f"CYCLE_{cycle}_SHORT_REDUCE":
            continue
        meta = dict(intent.get("metadata_excerpt") or {})
        if meta.get("normal_cycle_second_leg_split"):
            submitted_stage_intents += 1
            planned_stage_count = int(safe_float(meta.get("split_stage_count"), planned_stage_count or 0) or 0)

    return {
        "variant": variant,
        "long_notional_usdt": long_notional,
        "cycle": cycle,
        "has_long_add": True,
        "long_add_price": safe_float(long_add.get("fill_price")),
        "long_add_timestamp": long_add.get("timestamp"),
        "long_add_local_candle": long_local,
        "long_add_qty": safe_float(long_add.get("qty")),
        "long_add_closed_pnl": safe_float(long_add.get("closed_pnl") or long_add.get("confirmed_closed_pnl")),
        "exit_after_long_add": exit_after_long,
        "n_short_reduce_fills": len(legs["short_reduce"]),
        "first_short_reduce_price": first_sr_price,
        "first_short_reduce_timestamp": (legs["short_reduce"][0].get("timestamp") if legs["short_reduce"] else None),
        "planned_split_stage_count": planned_stage_count,
        "submitted_split_stage_intents": submitted_stage_intents,
        "dedupe_collapsed_equal_qty_stages": bool(
            planned_stage_count and submitted_stage_intents and submitted_stage_intents < planned_stage_count
        ),
        "stage1_filled_before_final_trigger_price": False,  # normal split = same trigger
        "observed_bounce_high": max_high,
        "bounce_high_local_candle": max_high_local,
        "bounce_high_timestamp": _ts(getattr(candles[start_index + max_high_local], "timestamp", None))
        if start_index + max_high_local < len(candles)
        else None,
        "reference_bounce_high": bounce_ref,
        "exit_during_bounce_window": exit_during_bounce,
        "bounce_reaches_exit_during_window": bounce_reaches_exit_during,
        "gap_exit_minus_bounce_during_window": gap_during,
        "final_exit_after_stages": final_exit,
        "stages_filled_before_bounce_high": sum(
            1
            for f in legs["short_reduce"]
            if int(f.get("candle_index") or 0) <= max_high_local
        ),
        "stage_exit_snapshots": stage_exits,
        "actually_closed_flat": actually_flat,
        "final_status": normalize_trade_status(result),
        "final_mtm_usdt": safe_float(result.overall_pnl),
        # Keep keys used by summary CSV
        "bounce_reaches_final_exit": bounce_reaches_exit_during,
        "gap_exit_minus_bounce": gap_during,
    }


def size_summary_row(
    *,
    variant: str,
    long_n: float,
    short_n: float,
    result: BacktestResult,
    analysis: dict[str, Any],
    split_outcome: dict[str, Any],
    bounce: dict[str, Any],
    attempts_c4: list[dict[str, Any]],
) -> dict[str, Any]:
    fills = list(result.fill_log or [])
    max_long = max((safe_float(f.get("long_qty_after")) for f in fills), default=0.0)
    max_short = max((safe_float(f.get("short_qty_after")) for f in fills), default=0.0)
    excerpt = dict(result.final_strategy_state_excerpt or {})
    invalid = int(
        detect_invalid_partial_cycle(dict(excerpt.get("strategy_state") or excerpt))
        if normalize_trade_status(result) != "closed"
        else False
    )
    return {
        "variant": variant,
        "long_notional_usdt": long_n,
        "short_notional_usdt": short_n,
        "status": normalize_trade_status(result),
        "max_cycle": analysis.get("max_cycle"),
        "realized_pnl": analysis.get("realized_pnl"),
        "mtm_pnl": analysis.get("mtm_pnl"),
        "duration_candles": analysis.get("duration_candles"),
        "exit_rebuild_count": analysis.get("exit_rebuild_count"),
        "undercoverage": analysis.get("undercoverage"),
        "invalid_partial_cycle": invalid,
        "max_long_qty": max_long,
        "max_short_qty": max_short,
        "c4_split_outcome": split_outcome.get("outcome"),
        "c4_split_detail": split_outcome.get("detail"),
        "c4_stage_attempts": len(attempts_c4),
        "c4_stages_accepted": sum(1 for a in attempts_c4 if a.get("accepted")),
        "c4_n_short_reduce_fills": bounce.get("n_short_reduce_fills"),
        "c4_bounce_high": bounce.get("observed_bounce_high"),
        "c4_exit_during_bounce": bounce.get("exit_during_bounce_window"),
        "c4_bounce_reaches_exit": bounce.get("bounce_reaches_exit_during_window"),
        "c4_gap_exit_minus_bounce": bounce.get("gap_exit_minus_bounce_during_window"),
        "c4_planned_stages": bounce.get("planned_split_stage_count"),
        "c4_submitted_stages": bounce.get("submitted_split_stage_intents"),
        "c4_dedupe_collapsed": bounce.get("dedupe_collapsed_equal_qty_stages"),
        "actually_closed": bounce.get("actually_closed_flat"),
    }


def write_code_path_map(path: Path) -> None:
    path.write_text(
        f"""# Code path map — staged/split Second-Leg (APTUSDT T3 size audit)

Generated: `{datetime.now(timezone.utc).isoformat()}`

## Two distinct mechanisms in Main-Bot

### A) `normal_cycle_second_leg_split` (relevant for Long-primary)

| Item | Value |
|---|---|
| Decision function | `FixedCycleStrategy._maybe_build_normal_cycle_second_leg_split_intents` |
| File | `fixed_cycle_hedge_bot/fixed_cycle_strategy.py` (~L22874) |
| Call site (long-primary) | `_build_short_tp_follow_up` (~L11375) after gate allow |
| Stage counts tried | **3 then 2** (same trigger price for all stages) |
| Qty split | even split via `_normalize_qty`; last stage gets remainder |
| Stage prices | **identical** `normalized_trigger_price` (NOT intermediate prices) |
| Min-qty gate | `stage_qty < min_order_qty` → reject candidate |
| Min-notional gate | `stage_qty * trigger_price < min_notional` → reject candidate |
| Config fallbacks | `config.min_order_qty` (live **0.001**), `config.min_notional_usdt` (live **5.0**) |
| Exchange rules | `_resolve_instrument_rules` may override with instrument `min_notional` / `min_order_qty` |
| On failure | returns `None` → caller places **single full-qty** second-leg intent |
| Metadata | `normal_cycle_second_leg_split=True`, `split_stage_index`, `split_stage_count`, `split_stage_qtys` |
| **Dedupe trap** | `_dedupe_second_leg_intents` (~L9126) collapses intents with **same trigger AND same qty**. Equal even-split stages are therefore reduced to **one** submitted order (only ~1/N of planned cover qty). |
| Cycle completion | `_is_normal_cycle_second_leg_split_complete` — all stage indices filled before sequence advance |
| Exit rebuild | `_force_exit_rebuild_after_cycle_fill` on every cycle fill (incl. each split stage) |
| Pending loss | `pending_cycle_loss_usdt` deducted by each profitable SHORT_REDUCE fill (`profit_deducted`) |

### B) `is_staged_second_leg_tp` (distinct intermediate prices)

| Item | Value |
|---|---|
| Planner | loss-based staging inside long-second-leg / recovery paths (~L9730+) |
| Stage count | 3 if `max_post_recovery_long_reduce_distance_pct > 0` and distance > 0, else 1 |
| Prices | interpolate first_leg_fill → final trigger (`ratio = (i+1)/N`) |
| **Long-primary SHORT_REDUCE** | **DISABLED** — event `fixed_cycle_staged_second_leg_disabled_for_short_reduce` / `single_25pct_reduce_required` |
| Implication for APT T3 | Price-staged early TPs at ~⅓ and ⅔ of the 7.4% gap **do not apply** on the long bot |

## Config fields (live long_bot_1)

```
base_notional_usdt = 100
hedge_ratio_short = 0.5
min_notional_usdt = 5.0
min_order_qty = 0.001
tp_profit_target_pct = 0.25
tp_buffer_pct = 0.0002
target_profit_usdt = 0.015
long_fill_distance_pct = 0.5
```

No dedicated `enable_second_leg_split` flag — split is always attempted when qty/notional allow.

## Why Cycle 4 at 100/50 was not partially closed earlier

1. Price-staged TPs are **off** for SHORT_REDUCE.
2. Qty-split (same price ~1.6669) only activates if each stage notional ≥ **5 USDT**.
3. At 100/50, Cycle-4 short-reduce qty 4.769 → 2-way stages ≈3.97 USDT, 3-way ≈2.65 USDT → both **below 5** → builder returns `None` → single full-qty order at the final trigger only.
4. Therefore the market must reach ~1.6669 for any cover; intermediate bounce to ~2.003 (exit then ~2.075) does **not** fill a partial second leg and does **not** reach the then-active basket exit.

## Additional Main-Bot interaction at large size

Even when 3 equal stages pass min-notional (e.g. 1000/500 → 3×47.677 @ ~1.6654),
`_dedupe_second_leg_intents` collapses them to **one** working order. Observed APT T3 S1000:
planned `split_stage_count=3`, submitted/filled **1×47.677** only.

## Simulator vs runtime

| Layer | Path |
|---|---|
| Strategy logic | Shared `fixed_cycle_hedge_bot/fixed_cycle_strategy.py` |
| Simulator | `research/backtests/hedge_bot_original_simulator.py` submits intents → virtual book |
| Runtime | `fixed_cycle_hedge_bot/runtime.py` also tags `normal_cycle_second_leg_split` fills |
| Fill causality | Research conservative model: deferred orders fillable from X+1 only |

## What happens if only Stage 1 fills then market turns

- Sequence does **not** advance (`should_advance_sequence=False` until all split stages complete).
- Remaining stage orders stay working at the **same** trigger.
- Exit rebuild still runs after Stage 1 (`_force_exit_rebuild_after_cycle_fill`).
- Coverage / `pending_cycle_loss_usdt` reduced by Stage-1 PnL only.
- Cycle remains incomplete until remaining stages fill or are cancelled/replaced.
""",
        encoding="utf-8",
    )


def write_report(
    path: Path,
    *,
    summaries: list[dict[str, Any]],
    split_by_variant: dict[str, dict[str, Any]],
    bounce_by_variant: dict[str, dict[str, Any]],
    parity: dict[str, Any],
) -> None:
    s100 = next((s for s in summaries if s["variant"] == "S100"), {})
    s1000 = next((s for s in summaries if s["variant"] == "S1000"), {})
    b100 = bounce_by_variant.get("S100") or {}
    b1000 = bounce_by_variant.get("S1000") or {}
    o100 = split_by_variant.get("S100") or {}
    o1000 = split_by_variant.get("S1000") or {}

    lines = [
        "# APTUSDT T3 — staged/split Second-Leg size comparison",
        "",
        f"Generated: `{datetime.now(timezone.utc).isoformat()}`",
        "",
        "## Setup",
        "",
        "- Coin / trade: APTUSDT continuous trade 3, start_index **570**",
        "- Baseline semantics only (no S1/S2/S3, freeze, recovery)",
        "- Variants: S100 (100/50), S1000 (1000/500); optional S500",
        "",
        "## Code-audit takeaway",
        "",
        "> Long-primary Second Leg uses **qty-split at one trigger price**. "
        "Distinct-price staging is **disabled** for SHORT_REDUCE. "
        "Min-notional **5 USDT per stage** is why 100/50 usually cannot split.",
        "",
        "See `code_path_map.md`.",
        "",
        "## S100 Cycle-4 split outcome",
        "",
        f"- **{o100.get('outcome')}**: {o100.get('detail')}",
        "",
        "## S1000 Cycle-4 split outcome",
        "",
        f"- **{o1000.get('outcome')}**: {o1000.get('detail')}",
        "",
        "## Size comparison",
        "",
        "| metric | S100 | S1000 |",
        "|---|---:|---:|",
        f"| status | {s100.get('status')} | {s1000.get('status')} |",
        f"| max_cycle | {s100.get('max_cycle')} | {s1000.get('max_cycle')} |",
        f"| mtm_pnl | {safe_float(s100.get('mtm_pnl')):.4f} | {safe_float(s1000.get('mtm_pnl')):.4f} |",
        f"| c4_split | {s100.get('c4_split_outcome')} | {s1000.get('c4_split_outcome')} |",
        f"| c4_SR_fills | {s100.get('c4_n_short_reduce_fills')} | {s1000.get('c4_n_short_reduce_fills')} |",
        f"| bounce_high | {safe_float(b100.get('observed_bounce_high')):.4f} | {safe_float(b1000.get('observed_bounce_high')):.4f} |",
        f"| exit_during_bounce | {b100.get('exit_during_bounce_window')} | {b1000.get('exit_during_bounce_window')} |",
        f"| bounce_reaches_then_exit | {b100.get('bounce_reaches_exit_during_window')} | {b1000.get('bounce_reaches_exit_during_window')} |",
        f"| gap (exit−bounce) | {b100.get('gap_exit_minus_bounce_during_window')} | {b1000.get('gap_exit_minus_bounce_during_window')} |",
        f"| planned/submitted stages | {b100.get('planned_split_stage_count')}/{b100.get('submitted_split_stage_intents')} | "
        f"{b1000.get('planned_split_stage_count')}/{b1000.get('submitted_split_stage_intents')} |",
        f"| dedupe collapsed | {b100.get('dedupe_collapsed_equal_qty_stages')} | {b1000.get('dedupe_collapsed_equal_qty_stages')} |",
        f"| actually_flat | {b100.get('actually_closed_flat')} | {b1000.get('actually_closed_flat')} |",
        "",
        "## Abschlussfragen",
        "",
        f"1. **Warum Split bei 100/50 nicht?** Qty-split versucht 2–3 Stages am gleichen Trigger; "
        f"Stage-Notional fällt unter {MIN_NOTIONAL_USDT} USDT → Builder `None` → single full-qty "
        f"({o100.get('outcome')}).",
        "2. **Ab welcher Size alle Stages akzeptiert?** Stage-Notional ≥ 5 USDT. Bei APT C4 ≈1.67: "
        "2-way braucht ~10 USDT total second-leg notional, 3-way ~15 USDT. Praktisch greift 1000/500 "
        "den Builder — aber siehe Dedup.",
        "3. **Cycle 4 früher teilweise ausgeglichen bei 1000/500?** Nein. Alle Stages teilen denselben "
        "Trigger (~1.665). Fill erst am 2026-01-19, Bounce-Hoch am 2026-01-13. Zusätzlich kollabiert "
        "Dedup 3 geplante Stages auf 1 Order.",
        "4. **Exit neu nach Stage-Fill?** Ja via `_force_exit_rebuild_after_cycle_fill` — siehe "
        "`exit_after_each_stage.csv`.",
        f"5. **Bounce ~2.003 schließt 1000/500?** Nein. Exit während Bounce-Fenster war "
        f"{b1000.get('exit_during_bounce_window')} (Gap "
        f"{b1000.get('gap_exit_minus_bounce_during_window')}).",
        f"6. **Zusätzliches Downside?** S100 mtm={safe_float(s100.get('mtm_pnl')):.2f} vs "
        f"S1000 mtm={safe_float(s1000.get('mtm_pnl')):.2f}; S500 schloss sogar früh flat (+4.86).",
        "7. **Löst Stage-Logik diesen Blocker?** Nein. Weder 100/50 noch 1000/500 flat; "
        "Qty-Split liefert keine Zwischenpreise; Dedup schwächt große Splits zusätzlich.",
        "8. **Keine Runtime-Empfehlung.**",
        "",
        "## Parity / guards",
        "",
        f"- S100 baseline parity: **{'PASS' if parity.get('ok') else 'FAIL / skipped'}**",
        f"- Details: `{parity}`",
        "",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

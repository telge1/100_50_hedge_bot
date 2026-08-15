"""APTUSDT Trade-3 SHORT_REDUCE multi-price staging lab helpers (research-only)."""

from __future__ import annotations

import json
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
    check_baseline_parity,
)
from research.backtests.backtest_report import BacktestResult
from research.backtests.historical_backtest import run_historical_backtest
from research.backtests.inventory_mtm_freeze import inventory_mtm_usdt, safe_float
from research.backtests.long_add_multistart_metrics import analyze_trade, normalize_trade_status
from research.backtests.run_inventory_mtm_neg1_policy_audit import (
    BASELINE_DIR,
    FILL_MODEL,
    LONG_FILL_DISTANCE_PCT,
    TARGET_PROFIT_USDT,
    TP_PROFIT_TARGET_PCT,
)
from research.backtests.safe_cycle_boundary_freeze import detect_invalid_partial_cycle
from research.backtests.second_leg_price_staging import (
    SecondLegPriceStagingConfig,
    resolve_profile,
)

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUT = ROOT / "research/backtests/results/apt_t3_short_reduce_price_staging_lab_20260721"
PROTECTED = (
    BASELINE_DIR,
    ROOT / "research/backtests/results/safe_cycle_boundary_freeze_audit_20260720",
    ROOT / "research/backtests/results/long_baseline_1000_500_stage_tp_audit_20260721",
    ROOT / "research/backtests/results/apt_baseline_blocker_root_cause_20260721",
    ROOT / "research/backtests/results/apt_t3_stage_tp_size_comparison_20260721",
    ROOT / "research/backtests/results/second_leg_price_staging_code_audit_20260721",
)

BOUNCE_HIGH = 2.0031
CYCLE_FOCUS = 4


def assert_output_dir_safe(output_dir: Path) -> None:
    resolved = output_dir.resolve()
    for protected in PROTECTED:
        if resolved == protected.resolve():
            raise RuntimeError(f"refusing protected output dir: {protected}")
    if output_dir.exists() and any(output_dir.iterdir()):
        raise FileExistsError(f"refusing to overwrite non-empty output dir: {output_dir}")


def parse_sizes(spec: str) -> list[tuple[str, float, float]]:
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


def parse_profiles(spec: str) -> list[SecondLegPriceStagingConfig]:
    names = [p.strip() for p in str(spec or "").split(",") if p.strip()]
    if not names:
        names = ["legacy"]
    return [resolve_profile(n) for n in names]


def run_lab_backtest(
    *,
    candles: list[Any],
    start_index: int,
    base_notional_usdt: float,
    staging_config: SecondLegPriceStagingConfig,
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
        second_leg_price_staging_config=staging_config if staging_config.enabled else None,
    )
    result.start_index = start_index
    result.trade_number = APT_TRADE3_ID
    return result


def _cycle_short_reduce_intents(result: BacktestResult, cycle: int = CYCLE_FOCUS) -> list[dict[str, Any]]:
    purpose = f"CYCLE_{cycle}_SHORT_REDUCE"
    rows: list[dict[str, Any]] = []
    for intent in result.intent_log or []:
        if str(intent.get("purpose") or "") != purpose:
            continue
        meta = dict(intent.get("metadata_excerpt") or intent.get("metadata") or {})
        rows.append({"intent": intent, "meta": meta})
    return rows


def stage_plan_rows(
    *,
    profile: str,
    size_label: str,
    long_notional: float,
    result: BacktestResult,
    cycle: int = CYCLE_FOCUS,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    intents = _cycle_short_reduce_intents(result, cycle)
    # Prefer first submission batch (lowest candle_index among intents if present)
    seen_idx: set[int] = set()
    for item in intents:
        intent = item["intent"]
        meta = item["meta"]
        stage_index = meta.get("stage_index")
        if stage_index is None and meta.get("split_stage_index") is not None:
            stage_index = meta.get("split_stage_index")
        if stage_index is None:
            stage_index = 0 if not seen_idx else max(seen_idx) + 1
        stage_index = int(stage_index)
        if stage_index in seen_idx and meta.get("research_price_staging"):
            continue
        seen_idx.add(stage_index)
        qty = safe_float(intent.get("qty"))
        trigger = safe_float(intent.get("trigger_price"))
        notional = qty * trigger
        accepted = True
        reject = None
        if meta.get("fallback_to_single_second_leg"):
            accepted = False
            reject = meta.get("split_fallback_reason") or "fallback_single"
        rows.append(
            {
                "profile": profile,
                "size": size_label,
                "long_notional_usdt": long_notional,
                "cycle": cycle,
                "stage_index": stage_index,
                "first_leg_fill": safe_float(meta.get("first_leg_fill_price")),
                "full_trigger": safe_float(
                    meta.get("final_second_leg_trigger_price") or meta.get("trigger_price") or trigger
                ),
                "planned_trigger": trigger,
                "stage_qty": qty,
                "stage_notional": notional,
                "expected_net": safe_float(meta.get("stage_expected_net")),
                "accepted": int(accepted),
                "reject_reason": reject,
                "is_price_staged": int(bool(meta.get("is_staged_second_leg_tp") or meta.get("research_price_staging"))),
                "is_qty_split_same_price": int(bool(meta.get("normal_cycle_second_leg_split"))),
                "price_fraction": safe_float(meta.get("price_fraction")),
                "qty_fraction": safe_float(meta.get("qty_fraction")),
            }
        )
    if not rows:
        rows.append(
            {
                "profile": profile,
                "size": size_label,
                "long_notional_usdt": long_notional,
                "cycle": cycle,
                "stage_index": None,
                "accepted": 0,
                "reject_reason": "no_cycle4_short_reduce_intent",
                "is_price_staged": 0,
            }
        )
    return rows


def stage_fill_rows(
    *,
    profile: str,
    size_label: str,
    long_notional: float,
    result: BacktestResult,
    candles: list[Any],
    start_index: int,
    cycle: int = CYCLE_FOCUS,
) -> list[dict[str, Any]]:
    purpose = f"CYCLE_{cycle}_SHORT_REDUCE"
    rows: list[dict[str, Any]] = []
    cum_coverage = 0.0
    required_total = None
    for fill in result.fill_log or []:
        if _purpose(fill) != purpose:
            continue
        meta = dict(fill.get("metadata_excerpt") or fill.get("metadata") or {})
        pnl = safe_float(fill.get("closed_pnl") or fill.get("confirmed_closed_pnl"))
        cum_coverage += pnl
        if required_total is None:
            required_total = safe_float(meta.get("stage_required_net_total"))
        local = int(fill.get("candle_index") or 0)
        abs_i = start_index + local
        rows.append(
            {
                "profile": profile,
                "size": size_label,
                "long_notional_usdt": long_notional,
                "cycle": cycle,
                "stage_index": meta.get("stage_index", meta.get("split_stage_index")),
                "fill_candle_local": local,
                "fill_candle_abs": abs_i,
                "fill_timestamp": fill.get("timestamp"),
                "fill_price": safe_float(fill.get("fill_price")),
                "qty": safe_float(fill.get("qty")),
                "realized_stage_pnl": pnl,
                "cum_coverage": cum_coverage,
                "remaining_coverage_need": (
                    max((required_total or 0.0) - cum_coverage, 0.0) if required_total else None
                ),
                "short_qty_after": safe_float(fill.get("short_qty_after")),
                "long_qty_after": safe_float(fill.get("long_qty_after")),
                "net_exposure_after": safe_float(fill.get("long_qty_after"))
                - safe_float(fill.get("short_qty_after")),
            }
        )
    return rows


def annotate_fills_vs_bounce(
    fill_rows: list[dict[str, Any]],
    *,
    candles: list[Any],
    start_index: int,
    long_add_local: int | None,
) -> list[dict[str, Any]]:
    if long_add_local is None:
        for row in fill_rows:
            row["filled_before_bounce_high"] = None
        return fill_rows
    # Find first local candle after long_add where high >= BOUNCE_HIGH
    bounce_local = None
    for local in range(long_add_local + 1, len(candles) - start_index):
        abs_i = start_index + local
        if abs_i >= len(candles):
            break
        if _candle_high(candles[abs_i]) + 1e-9 >= BOUNCE_HIGH:
            bounce_local = local
            break
    for row in fill_rows:
        fl = row.get("fill_candle_local")
        row["bounce_high_first_local"] = bounce_local
        row["filled_before_bounce_high"] = (
            int(fl is not None and bounce_local is not None and int(fl) < int(bounce_local))
            if bounce_local is not None
            else None
        )
    return fill_rows


def exit_after_stage_rows(
    *,
    profile: str,
    size_label: str,
    long_notional: float,
    result: BacktestResult,
    candles: list[Any],
    start_index: int,
    cycle: int = CYCLE_FOCUS,
) -> list[dict[str, Any]]:
    purpose_add = f"CYCLE_{cycle}_LONG_ADD"
    purpose_sr = f"CYCLE_{cycle}_SHORT_REDUCE"
    order_log = list(result.order_log or [])
    rows: list[dict[str, Any]] = []
    cum = 0.0
    exit_before = None
    for fill in result.fill_log or []:
        purpose = _purpose(fill)
        if purpose not in (purpose_add, purpose_sr):
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
        if purpose == purpose_add:
            exit_before = active_exit
        mtm = inventory_mtm_usdt(
            realized=cum,
            long_qty=long_qty,
            long_avg=long_avg,
            short_qty=short_qty,
            short_avg=short_avg,
            mark=mark,
        )
        meta = dict(fill.get("metadata_excerpt") or {})
        rows.append(
            {
                "profile": profile,
                "size": size_label,
                "long_notional_usdt": long_notional,
                "cycle": cycle,
                "leg": "LONG_ADD" if purpose == purpose_add else "SHORT_REDUCE",
                "stage_index": meta.get("stage_index", meta.get("split_stage_index")),
                "local_candle": local,
                "exit_before_stage": exit_before if purpose == purpose_sr else None,
                "exit_after_stage": active_exit,
                "mtm_after_stage": mtm,
                "short_qty": short_qty,
                "long_qty": long_qty,
                "net_exposure": long_qty - short_qty,
                "cum_realized": cum,
            }
        )
        if purpose == purpose_sr:
            exit_before = active_exit
    return rows


def coverage_rows(exit_rows: list[dict[str, Any]], fill_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_stage = {r.get("stage_index"): r for r in fill_rows if r.get("stage_index") is not None}
    out: list[dict[str, Any]] = []
    for row in exit_rows:
        if row.get("leg") != "SHORT_REDUCE":
            continue
        fr = by_stage.get(row.get("stage_index")) or {}
        out.append(
            {
                **{k: row[k] for k in ("profile", "size", "long_notional_usdt", "cycle", "stage_index")},
                "realized_stage_pnl": fr.get("realized_stage_pnl"),
                "cum_coverage": fr.get("cum_coverage"),
                "remaining_coverage_need": fr.get("remaining_coverage_need"),
                "exit_after": row.get("exit_after_stage"),
                "mtm_after": row.get("mtm_after_stage"),
                "short_qty": row.get("short_qty"),
                "net_exposure": row.get("net_exposure"),
            }
        )
    return out


def bounce_analysis(
    *,
    profile: str,
    size_label: str,
    long_notional: float,
    result: BacktestResult,
    candles: list[Any],
    start_index: int,
    fill_rows: list[dict[str, Any]],
    exit_rows: list[dict[str, Any]],
    cycle: int = CYCLE_FOCUS,
) -> dict[str, Any]:
    long_adds = [
        f for f in (result.fill_log or []) if _purpose(f) == f"CYCLE_{cycle}_LONG_ADD"
    ]
    if not long_adds:
        return {
            "profile": profile,
            "size": size_label,
            "has_long_add": False,
        }
    long_local = int(long_adds[0].get("candle_index") or 0)
    order_log = list(result.order_log or [])
    exit_after_long = _active_exit_at_local_candle(order_log, local_candle=long_local)

    bounce_local = None
    for local in range(long_local + 1, len(candles) - start_index):
        abs_i = start_index + local
        if abs_i >= len(candles):
            break
        if _candle_high(candles[abs_i]) + 1e-9 >= BOUNCE_HIGH:
            bounce_local = local
            break

    stages_before = [
        r for r in fill_rows if r.get("filled_before_bounce_high") == 1
    ]
    exit_at_bounce = (
        _active_exit_at_local_candle(order_log, local_candle=bounce_local)
        if bounce_local is not None
        else None
    )
    bounce_reaches_exit = bool(
        exit_at_bounce is not None and bounce_local is not None and BOUNCE_HIGH + 1e-9 >= exit_at_bounce
    )
    # Also check max high in a window after long-add before first SR or +2000
    sr_fills = [f for f in (result.fill_log or []) if _purpose(f) == f"CYCLE_{cycle}_SHORT_REDUCE"]
    end_local = long_local + 2000
    if sr_fills:
        end_local = max(int(sr_fills[0].get("candle_index") or 0), long_local + 1)
    max_high = 0.0
    for local in range(long_local + 1, min(end_local + 1, len(candles) - start_index)):
        abs_i = start_index + local
        if abs_i >= len(candles):
            break
        max_high = max(max_high, _candle_high(candles[abs_i]))

    status = normalize_trade_status(result)
    analysis = analyze_trade(
        result,
        variant=f"{profile}_{size_label}",
        long_add_pct=0.5,
        target_profit_usdt=TARGET_PROFIT_USDT,
        window_candles=candles[start_index:],
        valid=True,
        skip_reason="ok",
    )
    excerpt = dict(result.final_strategy_state_excerpt or {})
    invalid = detect_invalid_partial_cycle(excerpt)

    capital_binding = safe_float(analysis.get("max_total_notional"))
    worst_mtm = None
    for row in exit_rows:
        m = row.get("mtm_after_stage")
        if m is None:
            continue
        m = safe_float(m)
        if worst_mtm is None or m < worst_mtm:
            worst_mtm = m

    return {
        "profile": profile,
        "size": size_label,
        "long_notional_usdt": long_notional,
        "cycle": cycle,
        "bounce_high_ref": BOUNCE_HIGH,
        "long_add_local": long_local,
        "bounce_first_local": bounce_local,
        "exit_after_long_add": exit_after_long,
        "exit_at_bounce": exit_at_bounce,
        "stages_filled_before_bounce": len(stages_before),
        "stage_indices_before_bounce": [r.get("stage_index") for r in stages_before],
        "bounce_reaches_active_exit": bounce_reaches_exit,
        "max_high_before_first_sr_or_window": max_high,
        "max_high_reaches_exit_after_long": bool(
            exit_after_long and max_high + 1e-9 >= exit_after_long
        ),
        "trade_flat": status == "closed",
        "final_status": status,
        "final_mtm": safe_float(analysis.get("mtm_pnl")),
        "worst_mtm": worst_mtm if worst_mtm is not None else safe_float(analysis.get("mtm_pnl")),
        "realized_pnl": safe_float(result.realized_pnl),
        "cycles_seen": result.cycles_seen,
        "invalid_partial": int(bool(invalid)),
        "gross_exposure": safe_float(analysis.get("max_total_notional")),
        "net_exposure": safe_float(analysis.get("final_net_qty")),
        "capital_binding_proxy": capital_binding,
        "undercoverage_flag": int(safe_float(analysis.get("undercoverage"))) > 0,
        "exit_rows_last_exit": exit_rows[-1].get("exit_after_stage") if exit_rows else None,
        "max_cycle": analysis.get("max_cycle"),
        "same_candle_long_add_short_reduce": analysis.get("same_candle_long_add_short_reduce"),
    }


def variant_summary_row(
    *,
    profile: str,
    size_label: str,
    long_notional: float,
    bounce: dict[str, Any],
    plan_rows: list[dict[str, Any]],
    fill_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    staged = any(int(r.get("is_price_staged") or 0) for r in plan_rows)
    n_stages = len([r for r in plan_rows if r.get("stage_index") is not None and int(r.get("accepted") or 0)])
    triggers = [safe_float(r.get("planned_trigger")) for r in plan_rows if r.get("planned_trigger")]
    return {
        "profile": profile,
        "size": size_label,
        "long_notional_usdt": long_notional,
        "price_staging_active": int(staged),
        "planned_stage_count": n_stages,
        "distinct_triggers": len({round(t, 6) for t in triggers if t > 0}),
        "stage_fills": len(fill_rows),
        "stages_before_bounce": bounce.get("stages_filled_before_bounce"),
        "exit_at_bounce": bounce.get("exit_at_bounce"),
        "bounce_reaches_exit": bounce.get("bounce_reaches_active_exit"),
        "trade_flat": bounce.get("trade_flat"),
        "final_status": bounce.get("final_status"),
        "final_mtm": bounce.get("final_mtm"),
        "worst_mtm": bounce.get("worst_mtm"),
        "realized_pnl": bounce.get("realized_pnl"),
        "cycles_seen": bounce.get("cycles_seen"),
        "invalid_partial": bounce.get("invalid_partial"),
        "undercoverage_flag": bounce.get("undercoverage_flag"),
        "gross_exposure": bounce.get("gross_exposure"),
        "net_exposure": bounce.get("net_exposure"),
    }


def implementation_diff_scope_md() -> str:
    return """# Implementation diff scope (research prototype)

## Added (research-only)

* `research/backtests/second_leg_price_staging.py` — config, profiles, planner, dedupe-by-stage_index
* `research/backtests/second_leg_price_staging_shim.py` — wraps `_build_short_tp_follow_up` **only when enabled=true**
* `research/backtests/apt_t3_short_reduce_price_staging_lab.py` — lab metrics
* `research/backtests/run_apt_t3_short_reduce_price_staging_lab.py` — CLI
* `research/backtests/test_second_leg_price_staging.py` — unit/guards

## Wired (opt-in kwargs, default None)

* `research/backtests/hedge_bot_original_simulator.py` — `second_leg_price_staging_config`
* `research/backtests/historical_backtest.py` — pass-through

## Not changed

* Live bot JSON / `FixedCycleHedgeConfig` defaults
* Main-bot SHORT_REDUCE disable path when shim not installed / enabled=false
* Legacy `_dedupe_second_leg_intents` (price+qty) when staging disabled
* No commit
"""


def write_report(
    path: Path,
    *,
    summaries: list[dict[str, Any]],
    bounce_by_key: dict[str, Any],
    parity: dict[str, Any],
) -> None:
    lines = [
        "# APTUSDT T3 SHORT_REDUCE multi-price staging lab",
        "",
        f"Bounce reference high: **{BOUNCE_HIGH}**",
        f"Focus cycle: **{CYCLE_FOCUS}**",
        f"Start index: **{APT_TRADE3_START_INDEX}**",
        "",
        "## Variant summary",
        "",
        "| profile | size | staged | stages | before bounce | exit@bounce | reaches | flat | final_mtm | worst_mtm | invalid_partial |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for s in summaries:
        lines.append(
            "| {profile} | {size} | {price_staging_active} | {planned_stage_count} | {stages_before_bounce} | {exit_at_bounce} | {bounce_reaches_exit} | {trade_flat} | {final_mtm} | {worst_mtm} | {invalid_partial} |".format(
                **{k: s.get(k) for k in (
                    "profile", "size", "price_staging_active", "planned_stage_count",
                    "stages_before_bounce", "exit_at_bounce", "bounce_reaches_exit",
                    "trade_flat", "final_mtm", "worst_mtm", "invalid_partial",
                )}
            )
        )
    lines.extend(
        [
            "",
            "## Abschlussfragen",
            "",
            "1. **Reicht Preis-Staging für den Bounce?** Siehe `bounce_reaches_exit` / `stages_before_bounce` in `bounce_reachability.json`.",
            "2. **Welche Stage-Fills senken den Exit am stärksten?** `exit_after_each_stage.csv`.",
            "3. **Welche Variante hält Short-Hedge offen?** Vergleich `short_qty` / `net_exposure` in coverage CSV (konservative frühen Fractions).",
            "4. **Undercoverage?** `undercoverage_flag` + Final Exit Coverage Gate / `parity_and_guards.json`.",
            "5. **Worst-MTM?** Spalte `worst_mtm` oben.",
            "6. **Technisch safe für späteren Runtime-Opt-in?** Nur hinter `enabled`; Legacy-Parität muss grün sein — noch **keine** Live-Empfehlung.",
            "7. **Keine Live-Empfehlung** in diesem Lab.",
            "",
            "## Parity (P0 legacy)",
            "",
            "```json",
            json.dumps(parity, indent=2, default=str),
            "```",
            "",
        ]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

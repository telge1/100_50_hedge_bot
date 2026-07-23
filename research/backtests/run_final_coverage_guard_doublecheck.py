#!/usr/bin/env python3
"""Final double-check of Variant-C basket-exit coverage guard (research-only).

Audits all ``covered_by_basket_exit`` cases from the targeted revalidation,
proves insufficient blocking + tolerance boundaries, runs race/legacy checks,
and writes artifacts under ``…/analysis/final_coverage_guard_doublecheck/``.

No strategy-economy changes. No full grid. No commit/push.
"""

from __future__ import annotations

import csv
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from research.backtests.candle_loader import load_candles_for_symbol
from research.backtests.historical_backtest import normalize_candles
from research.backtests.inventory_mtm_freeze import safe_float
from research.backtests.multicoin_blocker_price_staging import run_isolated_blocker
from research.backtests.run_c4_undercoverage_fix_validation import (
    DEFAULT_OUT as REVAL_OUT,
    IDENTITY_EPS,
    _capture_basket_close_economics,
    _classify_economic,
    _c4_cycle_pair_status,
    _fills,
    _identity,
    _restore_basket_coverage_method,
    _stage_events,
    _sum_fill_net_pnls,
    build_apt_t3_economics_doublecheck,
)
from research.backtests.second_leg_price_staging import (
    resolve_grid_profile,
    resolve_profile,
)

ROOT = Path(__file__).resolve().parents[2]
OUT = (
    ROOT
    / "research/backtests/results/multicoin_price_staging_grid_1000_500_20260721"
    / "analysis"
    / "final_coverage_guard_doublecheck"
)
REVAL_ROWS = REVAL_OUT / "revalidation_rows.csv"


def _load_covered_cases() -> list[dict[str, Any]]:
    rows = list(csv.DictReader(REVAL_ROWS.open(encoding="utf-8")))
    return [r for r in rows if int(safe_float(r.get("covered_by_basket_exit"))) == 1]


def _audit_one_case(row: dict[str, Any]) -> dict[str, Any]:
    coin = str(row["coin"]).upper()
    profile = str(row["profile"])
    start_index = int(safe_float(row["start_index"]))
    trade_number = int(safe_float(row["trade_number"]))
    cfg = (
        resolve_profile("legacy")
        if profile == "legacy"
        else resolve_grid_profile(profile)
    )
    candles = normalize_candles(coin, load_candles_for_symbol(coin, limit=50000))
    captures: list[dict[str, Any]] = []
    original = _capture_basket_close_economics(captures)
    try:
        result = run_isolated_blocker(
            coin=coin,
            candles=candles,
            start_index=start_index,
            staging_config=cfg,
            trade_number=trade_number,
        )
    finally:
        _restore_basket_coverage_method(original)

    capture = captures[-1] if captures else None
    economics_payload = build_apt_t3_economics_doublecheck(
        result=result, capture=capture
    )
    stage_info = _stage_events(result)
    cycle_pair = _c4_cycle_pair_status(result)
    economic_class = _classify_economic(
        status=str(getattr(result, "final_status", "") or ""),
        cycle_pair=cycle_pair,
        stage_info=stage_info,
    )
    fill_sum, fill_missing = _sum_fill_net_pnls(result)
    identities = economics_payload["identities"]
    all_pass = all(
        bool(identities[name].get("pass"))
        for name in (
            "min_required_identity",
            "expected_total_identity",
            "target_delta_identity",
            "sufficient_identity",
            "trade_pnl_identity",
        )
    )
    # basket_net identity also required
    all_pass = all_pass and bool(identities["basket_net_identity"].get("pass"))

    cancelled = stage_info.get("cancelled_stages") or []
    late = stage_info.get("late_stage_fills_after_exit") or []
    flat = str(getattr(result, "final_status", "")) == "closed"
    sufficient = bool((capture or {}).get("sufficient"))
    coverage_before_cancel_ok = bool(
        sufficient and flat and (not cancelled or not late)
    )

    return {
        "coin": coin,
        "trade_number": trade_number,
        "start_index": start_index,
        "profile": profile,
        "cycle": 4,
        "economic_class": economic_class,
        "final_status": getattr(result, "final_status", None),
        "exit_reason": getattr(result, "exit_reason", None),
        "trade_flat": int(flat),
        "filled_stages": stage_info.get("filled_stages"),
        "cancelled_rest_stages": cancelled,
        "late_stage_fills_after_exit": late,
        "orphan_stage_after_exit": int(bool(late)),
        "effective_pending_cycle_loss_usdt": (capture or {}).get(
            "effective_pending_cycle_loss_usdt"
        ),
        "target_profit_usdt": (capture or {}).get("target_profit_usdt"),
        "buffer_usdt": (capture or {}).get("buffer_usdt"),
        "min_required_total_usdt": (capture or {}).get("min_required_total_usdt"),
        "realized_cycle_net_usdt": (capture or {}).get("realized_cycle_net_usdt"),
        "basket_net_usdt": (capture or {}).get("basket_net_usdt"),
        "expected_total_net_after_exit": (capture or {}).get(
            "expected_total_net_after_exit"
        ),
        "target_delta_usdt": (capture or {}).get("target_delta_usdt"),
        "tolerance_usdt": (capture or {}).get("tolerance_usdt"),
        "sufficient": sufficient,
        "reason_code": (capture or {}).get("reason_code"),
        "realized_pnl": float(getattr(result, "realized_pnl", 0.0) or 0.0),
        "fill_net_pnl_sum": fill_sum,
        "fill_net_missing": fill_missing,
        "identities_all_pass": int(all_pass),
        "coverage_ok_before_rest_cancel": int(coverage_before_cancel_ok),
        "identities": identities,
        "unavailable_components": economics_payload.get("unavailable_components"),
        "capture_available": capture is not None,
    }


def _run_covered_case_audit() -> list[dict[str, Any]]:
    cases = _load_covered_cases()
    results: list[dict[str, Any]] = []
    for i, row in enumerate(cases, 1):
        print(
            f"[{i}/{len(cases)}] AUDIT {row['coin']} T{row['trade_number']} "
            f"{row['profile']}",
            flush=True,
        )
        results.append(_audit_one_case(row))
    return results


def _write_csv(path: Path, rows: list[dict[str, Any]], *, flat_keys: list[str]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=flat_keys, extrasaction="ignore")
        w.writeheader()
        for row in rows:
            out = {}
            for k in flat_keys:
                v = row.get(k)
                if isinstance(v, (list, dict)):
                    out[k] = json.dumps(v)
                else:
                    out[k] = v
            w.writerow(out)


def _identity_rows(case_results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for case in case_results:
        for name, identity in (case.get("identities") or {}).items():
            rows.append(
                {
                    "coin": case["coin"],
                    "trade_number": case["trade_number"],
                    "profile": case["profile"],
                    "identity": name,
                    "available": identity.get("available", True),
                    "pass": identity.get("pass"),
                    "lhs": identity.get("lhs"),
                    "rhs": identity.get("rhs")
                    if "calculated" not in identity
                    else identity.get("calculated"),
                    "stored": identity.get("stored"),
                    "difference": identity.get("difference"),
                }
            )
    return rows


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    t0 = time.time()

    # --- 1) covered_by_basket_exit audit ---
    case_results = _run_covered_case_audit()
    (OUT / "all_case_economics.json").write_text(
        json.dumps(case_results, indent=2, default=str), encoding="utf-8"
    )
    case_csv_keys = [
        "coin",
        "trade_number",
        "start_index",
        "profile",
        "cycle",
        "economic_class",
        "final_status",
        "trade_flat",
        "filled_stages",
        "cancelled_rest_stages",
        "late_stage_fills_after_exit",
        "orphan_stage_after_exit",
        "effective_pending_cycle_loss_usdt",
        "target_profit_usdt",
        "buffer_usdt",
        "min_required_total_usdt",
        "realized_cycle_net_usdt",
        "basket_net_usdt",
        "expected_total_net_after_exit",
        "target_delta_usdt",
        "tolerance_usdt",
        "sufficient",
        "reason_code",
        "realized_pnl",
        "fill_net_pnl_sum",
        "identities_all_pass",
        "coverage_ok_before_rest_cancel",
        "capture_available",
    ]
    _write_csv(OUT / "all_covered_by_basket_exit_cases.csv", case_results, flat_keys=case_csv_keys)
    identity_rows = _identity_rows(case_results)
    _write_csv(
        OUT / "identity_results.csv",
        identity_rows,
        flat_keys=[
            "coin",
            "trade_number",
            "profile",
            "identity",
            "available",
            "pass",
            "lhs",
            "rhs",
            "stored",
            "difference",
        ],
    )

    # Import synthetic/runtime proofs from the companion test module helpers.
    from research.backtests import final_coverage_guard_doublecheck_proofs as proofs

    insufficient = proofs.build_insufficient_block_case()
    (OUT / "insufficient_coverage_block_case.json").write_text(
        json.dumps(insufficient, indent=2, default=str), encoding="utf-8"
    )
    tolerance = proofs.build_tolerance_boundary_cases()
    (OUT / "tolerance_boundary_cases.json").write_text(
        json.dumps(tolerance, indent=2, default=str), encoding="utf-8"
    )
    races = proofs.build_runtime_race_results()
    (OUT / "runtime_race_results.json").write_text(
        json.dumps(races, indent=2, default=str), encoding="utf-8"
    )
    legacy = proofs.build_legacy_parity_check()
    (OUT / "legacy_parity.json").write_text(
        json.dumps(legacy, indent=2, default=str), encoding="utf-8"
    )

    economic_uc = sum(
        1
        for c in case_results
        if c.get("trade_flat") and not c.get("sufficient")
    )
    late_total = sum(len(c.get("late_stage_fills_after_exit") or []) for c in case_results)
    orphan_total = sum(int(c.get("orphan_stage_after_exit") or 0) for c in case_results)
    identities_ok = all(int(c.get("identities_all_pass") or 0) for c in case_results)
    all_sufficient = all(bool(c.get("sufficient")) for c in case_results)
    all_flat = all(int(c.get("trade_flat") or 0) == 1 for c in case_results)

    success = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "elapsed_sec": round(time.time() - t0, 2),
        "covered_by_basket_exit_cases": len(case_results),
        "all_cases_sufficient": all_sufficient,
        "all_identities_pass": identities_ok,
        "all_cases_flat": all_flat,
        "economic_undercoverage_closed": economic_uc,
        "late_stage_fill_after_exit": late_total,
        "orphan_stage_order": orphan_total,
        "invalid_partial": int(legacy.get("invalid_partial_sum") or 0),
        "over_close": int(legacy.get("over_close_sum") or 0),
        "duplicate_stage": int(legacy.get("duplicate_stage_sum") or 0),
        "legacy_parity": bool(legacy.get("legacy_parity")),
        "insufficient_block_proven": bool(insufficient.get("pass")),
        "tolerance_boundary_pass": bool(tolerance.get("pass")),
        "runtime_races_pass": bool(races.get("pass")),
        "no_closed_with_sufficient_false": economic_uc == 0,
    }
    success["all_pass"] = all(
        [
            success["all_cases_sufficient"],
            success["all_identities_pass"],
            success["economic_undercoverage_closed"] == 0,
            success["late_stage_fill_after_exit"] == 0,
            success["orphan_stage_order"] == 0,
            success["invalid_partial"] == 0,
            success["over_close"] == 0,
            success["duplicate_stage"] == 0,
            success["legacy_parity"],
            success["insufficient_block_proven"],
            success["tolerance_boundary_pass"],
            success["runtime_races_pass"],
        ]
    )
    # Decision answers
    success["decisions"] = {
        "basket_compensation_cases_correctly_covered": bool(
            success["all_cases_sufficient"] and success["all_identities_pass"]
        ),
        "true_undercoverage_reliably_blocked": bool(
            success["insufficient_block_proven"] and success["tolerance_boundary_pass"]
        ),
        "same_candle_and_runtime_races_secured": bool(success["runtime_races_pass"]),
        "legacy_unchanged": bool(success["legacy_parity"]),
        "blocker_before_next_candidate_eval": (
            None
            if success["all_pass"]
            else "see REPORT.md failing criteria"
        ),
        "two_early_medium_technically_load_bearing": bool(success["all_pass"]),
    }
    (OUT / "success_criteria.json").write_text(
        json.dumps(success, indent=2), encoding="utf-8"
    )

    # REPORT
    lines = [
        "# Final Coverage Guard Double-Check",
        "",
        f"Generated: `{success['generated_at']}`",
        f"Elapsed: `{success['elapsed_sec']}s`",
        "",
        "## Verdict",
        "",
        f"**all_pass = {success['all_pass']}**",
        "",
        "1. Basket-Kompensationsfälle korrekt gedeckt? "
        f"**{success['decisions']['basket_compensation_cases_correctly_covered']}**",
        "2. Echte Unterdeckung zuverlässig blockiert? "
        f"**{success['decisions']['true_undercoverage_reliably_blocked']}**",
        "3. Same-Candle-/Runtime-Races abgesichert? "
        f"**{success['decisions']['same_candle_and_runtime_races_secured']}**",
        f"4. Legacy unverändert? **{success['decisions']['legacy_unchanged']}**",
        "5. Blocker vor nächster Kandidatenbewertung? "
        f"**{'nein' if success['decisions']['blocker_before_next_candidate_eval'] is None else success['decisions']['blocker_before_next_candidate_eval']}**",
        "6. `two_early_medium` technisch belastbar? "
        f"**{success['decisions']['two_early_medium_technically_load_bearing']}**",
        "",
        "### Safety invariants",
        "",
        f"- economic_undercoverage_closed = **{success['economic_undercoverage_closed']}**",
        f"- invalid_partial / over_close / duplicate_stage = "
        f"**{success['invalid_partial']} / {success['over_close']} / {success['duplicate_stage']}**",
        f"- late_stage_fill_after_exit / orphan_stage_order = "
        f"**{success['late_stage_fill_after_exit']} / {success['orphan_stage_order']}**",
        f"- no closed with sufficient=false: **{success['no_closed_with_sufficient_false']}**",
        "",
        "## Covered-by-basket-exit audit",
        "",
        f"- cases: **{len(case_results)}**",
        f"- all sufficient: **{all_sufficient}**",
        f"- all identities pass: **{identities_ok}**",
        f"- late/orphan stage fills: **{late_total}/{orphan_total}**",
        f"- economic_undercoverage_closed: **{economic_uc}**",
        "",
        "Cases:",
        "",
    ]
    for c in case_results:
        lines.append(
            f"- `{c['coin']}` T{c['trade_number']} `{c['profile']}`: "
            f"delta={c.get('target_delta_usdt')}, tol={c.get('tolerance_usdt')}, "
            f"filled={c.get('filled_stages')}, cancelled={c.get('cancelled_rest_stages')}, "
            f"reason=`{c.get('reason_code')}`"
        )
    lines.extend(
        [
            "",
            "## Insufficient + tolerance",
            "",
            f"- insufficient block: **{insufficient.get('pass')}** "
            f"({insufficient.get('reason_code')})",
            f"- tolerance boundaries: **{tolerance.get('pass')}**",
            "",
            "## Races / Legacy / Safety",
            "",
            f"- runtime races: **{races.get('pass')}**",
            f"- legacy_parity: **{legacy.get('legacy_parity')}**",
            f"- invalid_partial/over_close/duplicate_stage: "
            f"**{success['invalid_partial']}/{success['over_close']}/{success['duplicate_stage']}**",
            "",
            "## Artifacts",
            "",
            f"- `{OUT / 'all_covered_by_basket_exit_cases.csv'}`",
            f"- `{OUT / 'all_case_economics.json'}`",
            f"- `{OUT / 'identity_results.csv'}`",
            f"- `{OUT / 'insufficient_coverage_block_case.json'}`",
            f"- `{OUT / 'tolerance_boundary_cases.json'}`",
            f"- `{OUT / 'runtime_race_results.json'}`",
            f"- `{OUT / 'legacy_parity.json'}`",
            f"- `{OUT / 'success_criteria.json'}`",
            "",
        ]
    )
    (OUT / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps(success, indent=2), flush=True)
    print(f"Wrote {OUT}", flush=True)


if __name__ == "__main__":
    main()

"""APT audit: start-distance guard under causal execution timing (T0–T3)."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from research.backtests.candle_loader import DEFAULT_DATA_DIR, load_candles_for_symbol
from research.backtests.multicoin_price_staging_grid import (
    atomic_write_json,
    atomic_write_text,
    write_csv,
)

from .config import CoberturaConfig
from .engine import EngineResult, _parse_ts
from .run_apt_start_and_post_add_distance_audit import (
    FP_BASELINE,
    HANDOFF_DIR,
    STRATEGY,
    load_pre_neutralization_book,
    neutralize_at_price,
)
from .runner import run_cobertura
from .start_distance import select_start_by_timing_mode

DEFAULT_OUTPUT_DIR = Path(
    "research/backtests/cobertura_0_notional_strategie/results/"
    "apt_start_distance_execution_timing_audit_20260726"
)

TIMING_MODES = ("T0", "T1", "T2", "T3")
THRESHOLDS = [0.050, 0.055, 0.060, 0.065, 0.070, 0.075, 0.080]

FP_WINNER_T0_6 = {
    "final_state": "RECOVERED",
    "fill_timestamp_prefix": "2026-01-19T00:05:00",
    "fill_price": 1.6447,
    "realized_overlay_pnl": 46.150,
    "final_total_exit_economics": 21.858,
    "recovery_rounds": 8,
}


def _approx(a: Any, b: Any, rel: float = 1e-3, abs_tol: float = 1e-6) -> bool:
    if isinstance(a, str) or isinstance(b, str):
        return str(a) == str(b)
    aa, bb = float(a), float(b)
    return abs(aa - bb) <= max(abs_tol, rel * max(abs(aa), abs(bb)))


def build_cfg(
    *,
    variant_id: str,
    neut_book: dict[str, float],
    start_ts: str,
    start_price: float,
) -> CoberturaConfig:
    raw: dict[str, Any] = {
        **STRATEGY,
        **neut_book,
        "start_timestamp": start_ts,
        "start_price": float(start_price),
        "minimum_start_distance_pct": None,
        "minimum_post_add_distance_pct": None,
        "post_add_distance_policy": "disabled",
        "output_dir": None,
        "run_id": variant_id,
        "tags": {
            "audit": "start_distance_execution_timing",
            "variant_id": variant_id,
            "tem_orders_imported": False,
            "fresh_initial_entry_required": False,
        },
    }
    return CoberturaConfig.from_dict(raw)


def metrics_from_run(
    *,
    variant_id: str,
    timing_mode: str,
    threshold: float | None,
    sel: dict[str, Any] | None,
    result: EngineResult,
) -> dict[str, Any]:
    fills = list(result.fills_events)
    adds = [f for f in fills if f.get("kind") == "overlay_short_add"]
    bes = [f for f in fills if f.get("kind") == "overlay_be_close"]
    last_econ = (
        result.total_exit_economics_timeline[-1]
        if result.total_exit_economics_timeline
        else {}
    )
    max_ov = 0.0
    max_gross = 0.0
    for row in result.per_bar_trace:
        max_ov = max(max_ov, float(row.get("overlay_short_qty") or 0.0))
        max_gross = max(max_gross, float(row.get("gross_notional") or 0.0))
    recovery_ts = None
    if result.state in ("RECOVERED", "RECOVERED_BE"):
        for row in reversed(result.per_bar_trace):
            if row.get("state") in ("RECOVERED", "RECOVERED_BE"):
                recovery_ts = row.get("timestamp")
                break
    sel = sel or {}
    return {
        "variant_id": variant_id,
        "timing_mode": timing_mode,
        "minimum_start_distance_pct": threshold,
        "trigger_timestamp": sel.get("trigger_timestamp"),
        "trigger_observation_price": sel.get("trigger_observation_price"),
        "trigger_observation_kind": sel.get("trigger_observation_kind"),
        "fill_timestamp": sel.get("fill_timestamp"),
        "fill_price": sel.get("fill_price"),
        "projected_short_avg_at_fill": sel.get("projected_short_avg_at_fill"),
        "projected_distance_at_fill": sel.get("projected_distance_at_fill"),
        "delay_bars": sel.get("delay_bars"),
        "delay_minutes": sel.get("delay_minutes"),
        "same_bar_fill": sel.get("same_bar_fill"),
        "used_low_as_fill": sel.get("used_low_as_fill", False),
        "final_state": result.state,
        "exit_reason": result.exit_reason,
        "recovered": result.state in ("RECOVERED", "RECOVERED_BE"),
        "recovery_timestamp": recovery_ts,
        "bars_processed": int(result.bars_processed),
        "recovery_rounds": int(result.recovery_rounds),
        "overlay_add_fills": len(adds),
        "overlay_be_closes": len(bes),
        "realized_overlay_pnl": float(result.ledger.realized_overlay_pnl),
        "cumulative_entry_fees": float(result.ledger.cumulative_entry_fees),
        "cumulative_close_fees": float(result.ledger.cumulative_close_fees),
        "final_total_exit_economics": last_econ.get("total_exit_economics"),
        "max_overlay_qty": max_ov,
        "max_total_gross_notional": max_gross,
        "tem_orders_imported": False,
        "initial_entry_created": False,
    }


def run_baseline(
    *,
    candles: list[dict[str, Any]],
    book: dict[str, float],
) -> tuple[dict[str, Any], EngineResult]:
    fill_ts = str(book["signal_available_ts"])
    # Baseline: immediate neutralization at signal open.
    sig = _parse_ts(fill_ts)
    fill_px = None
    for c in candles:
        if _parse_ts(c["timestamp"]) == sig:
            fill_px = float(c["open"])
            break
    if fill_px is None:
        raise ValueError("signal candle missing")
    neut = neutralize_at_price(book, fill_px)
    cfg = build_cfg(
        variant_id="baseline_immediate_0000",
        neut_book={
            "core_long_qty": neut["core_long_qty"],
            "core_long_avg": neut["core_long_avg"],
            "core_short_qty": neut["core_short_qty"],
            "core_short_avg": neut["core_short_avg"],
        },
        start_ts=sig.isoformat(),
        start_price=fill_px,
    )
    result = run_cobertura(cfg, candles=candles, write_outputs=False)
    sel = {
        "trigger_timestamp": sig.isoformat(),
        "trigger_observation_price": fill_px,
        "trigger_observation_kind": "open",
        "fill_timestamp": sig.isoformat(),
        "fill_price": fill_px,
        "projected_short_avg_at_fill": neut["core_short_avg"],
        "projected_distance_at_fill": (neut["core_short_avg"] - fill_px)
        / neut["core_short_avg"],
        "delay_bars": 0,
        "delay_minutes": 0,
        "same_bar_fill": True,
        "used_low_as_fill": False,
    }
    metrics = metrics_from_run(
        variant_id="baseline_immediate_0000",
        timing_mode="BASELINE",
        threshold=None,
        sel=sel,
        result=result,
    )
    return metrics, result


def run_timed_variant(
    *,
    candles: list[dict[str, Any]],
    book: dict[str, float],
    timing_mode: str,
    threshold: float,
) -> tuple[dict[str, Any], EngineResult, dict[str, Any]]:
    sel = select_start_by_timing_mode(
        candles,
        signal_ts=book["signal_available_ts"],
        existing_short_qty=book["short_qty"],
        existing_short_avg=book["short_avg"],
        neutralization_qty=book["neutralization_qty"],
        minimum_start_distance_pct=threshold,
        timing_mode=timing_mode,
        parse_ts=_parse_ts,
    )
    if sel.get("used_low_as_fill"):
        raise RuntimeError("integrity: low used as fill")
    if timing_mode == "T3" and abs(float(sel["fill_price"]) - float(sel["trigger_observation_price"])) <= 1e-15:
        # Fill coincidentally equals low only if next open == prior low; still OK
        # as long as fill source is open. Record audit flag.
        sel["fill_equals_trigger_low_coincidentally"] = True
    if timing_mode in ("T1", "T2", "T3") and sel.get("same_bar_fill"):
        raise RuntimeError(f"{timing_mode} must not same-bar fill")

    fill_px = float(sel["fill_price"])
    fill_ts = str(sel["fill_timestamp"])
    neut = neutralize_at_price(book, fill_px)
    vid = f"{timing_mode}_thr_{threshold:.3f}".replace(".", "p")
    cfg = build_cfg(
        variant_id=vid,
        neut_book={
            "core_long_qty": neut["core_long_qty"],
            "core_long_avg": neut["core_long_avg"],
            "core_short_qty": neut["core_short_qty"],
            "core_short_avg": neut["core_short_avg"],
        },
        start_ts=fill_ts,
        start_price=fill_px,
    )
    result = run_cobertura(cfg, candles=candles, write_outputs=False)
    metrics = metrics_from_run(
        variant_id=vid,
        timing_mode=timing_mode,
        threshold=threshold,
        sel=sel,
        result=result,
    )
    return metrics, result, sel


def decide(rows: list[dict[str, Any]], *, fp_ok: bool, winner_ok: bool) -> str:
    if not fp_ok or not winner_ok:
        return "APT_START_DISTANCE_EXECUTION_FAIL"

    def rec(mode: str, thr: float) -> bool:
        for r in rows:
            if (
                r.get("timing_mode") == mode
                and r.get("minimum_start_distance_pct") is not None
                and abs(float(r["minimum_start_distance_pct"]) - thr) < 1e-12
            ):
                return bool(r.get("recovered"))
        return False

    t0_6 = rec("T0", 0.06)
    t1_6 = rec("T1", 0.06)
    t2_6 = rec("T2", 0.06)
    t3_6 = rec("T3", 0.06)

    cons_modes = ("T1", "T2")
    cons_recover_any = any(
        r.get("recovered") and r.get("timing_mode") in cons_modes for r in rows
    )
    t0_only_6 = t0_6 and not t1_6 and not t2_6

    if t0_6 and t1_6 and t2_6:
        return "APT_START_DISTANCE_EXECUTION_ROBUST"
    if t0_only_6 and not cons_recover_any:
        return "APT_START_DISTANCE_SAME_OPEN_DEPENDENT"
    if t0_6 and cons_recover_any and (not t1_6 or not t2_6):
        # Live/open path works at 6%; conservative needs other thresholds.
        return "APT_START_DISTANCE_LIVE_ONLY"
    if t0_6 and not cons_recover_any:
        return "APT_START_DISTANCE_SAME_OPEN_DEPENDENT"
    # Non-monotonic / sparse recoveries
    recovered = [r for r in rows if r.get("recovered") and r.get("timing_mode") != "BASELINE"]
    if recovered:
        return "APT_START_DISTANCE_EXECUTION_UNSTABLE"
    return "APT_START_DISTANCE_EXECUTION_FAIL"


def run_audit(
    *,
    output_dir: Path,
    handoff_dir: Path = HANDOFF_DIR,
) -> dict[str, Any]:
    output_dir = Path(output_dir)
    if output_dir.exists() and (output_dir / "integrity.json").exists():
        raise FileExistsError(f"refusing to overwrite {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)

    book = load_pre_neutralization_book(handoff_dir)
    candles = load_candles_for_symbol(
        "APTUSDT", timeframe="5m", data_dir=DEFAULT_DATA_DIR, limit=50_000
    )

    all_rows: list[dict[str, Any]] = []
    triggers: list[dict[str, Any]] = []
    fill_audit: list[dict[str, Any]] = []

    base_m, _ = run_baseline(candles=candles, book=book)
    all_rows.append(base_m)

    for mode in TIMING_MODES:
        for thr in THRESHOLDS:
            metrics, _result, sel = run_timed_variant(
                candles=candles, book=book, timing_mode=mode, threshold=thr
            )
            all_rows.append(metrics)
            triggers.append(
                {
                    "variant_id": metrics["variant_id"],
                    "timing_mode": mode,
                    "minimum_start_distance_pct": thr,
                    "trigger_timestamp": sel["trigger_timestamp"],
                    "trigger_observation_kind": sel["trigger_observation_kind"],
                    "trigger_observation_price": sel["trigger_observation_price"],
                    "fill_timestamp": sel["fill_timestamp"],
                    "fill_price": sel["fill_price"],
                    "projected_distance_at_fill": sel["projected_distance_at_fill"],
                    "same_bar_fill": sel["same_bar_fill"],
                    "used_low_as_fill": sel["used_low_as_fill"],
                    "recovered": metrics["recovered"],
                }
            )
            fill_audit.append(
                {
                    "variant_id": metrics["variant_id"],
                    "timing_mode": mode,
                    "fill_is_open": True,
                    "fill_is_close": False,
                    "fill_is_low": False,
                    "fill_price": sel["fill_price"],
                    "trigger_kind": sel["trigger_observation_kind"],
                    "integrity_no_low_fill": not bool(sel["used_low_as_fill"]),
                    "integrity_t1_next_open": (
                        mode != "T1" or sel["same_bar_fill"] is False
                    ),
                    "integrity_t2_prior_close": (
                        mode != "T2"
                        or sel["trigger_observation_kind"] == "prior_close"
                    ),
                }
            )

    # Fingerprints
    fp_base_fails = []
    for k, exp in FP_BASELINE.items():
        if k not in base_m and k in (
            "overlay_add_fills",
            "overlay_be_closes",
        ):
            continue
        if k in base_m and not _approx(base_m.get(k), exp, rel=1e-6):
            # baseline metrics include overlay counts
            if k in base_m:
                fp_base_fails.append(f"{k}: got={base_m.get(k)} expected={exp}")
    # Explicit required baseline fields
    for k, exp in FP_BASELINE.items():
        got = base_m.get(k)
        if got is None:
            fp_base_fails.append(f"missing {k}")
        elif not _approx(got, exp, rel=1e-6):
            fp_base_fails.append(f"{k}: got={got} expected={exp}")
    fp_ok = not fp_base_fails

    winner = next(
        (
            r
            for r in all_rows
            if r["timing_mode"] == "T0"
            and r.get("minimum_start_distance_pct") is not None
            and abs(float(r["minimum_start_distance_pct"]) - 0.06) < 1e-12
        ),
        None,
    )
    winner_fails = []
    if winner is None:
        winner_fails.append("T0 6% missing")
    else:
        if winner["final_state"] != FP_WINNER_T0_6["final_state"]:
            winner_fails.append("state")
        if not str(winner.get("fill_timestamp", "")).startswith(
            FP_WINNER_T0_6["fill_timestamp_prefix"]
        ):
            winner_fails.append(
                f"fill_ts={winner.get('fill_timestamp')}"
            )
        if not _approx(winner.get("fill_price"), FP_WINNER_T0_6["fill_price"], rel=0, abs_tol=1e-9):
            winner_fails.append(f"fill_price={winner.get('fill_price')}")
        if not _approx(winner.get("realized_overlay_pnl"), FP_WINNER_T0_6["realized_overlay_pnl"], rel=1e-3):
            winner_fails.append("overlay_pnl")
        if not _approx(
            winner.get("final_total_exit_economics"),
            FP_WINNER_T0_6["final_total_exit_economics"],
            rel=1e-3,
        ):
            winner_fails.append("exit_econ")
        if int(winner.get("recovery_rounds") or -1) != FP_WINNER_T0_6["recovery_rounds"]:
            winner_fails.append("rounds")
    winner_ok = not winner_fails

    decision = decide(all_rows, fp_ok=fp_ok, winner_ok=winner_ok)

    # Summaries
    timing_summary = []
    for mode in TIMING_MODES:
        mode_rows = [r for r in all_rows if r["timing_mode"] == mode]
        recs = [r for r in mode_rows if r.get("recovered")]
        first = None
        if recs:
            first = min(recs, key=lambda r: float(r["minimum_start_distance_pct"]))
        timing_summary.append(
            {
                "timing_mode": mode,
                "n_variants": len(mode_rows),
                "n_recovered": len(recs),
                "first_recovering_threshold": (
                    None if first is None else first["minimum_start_distance_pct"]
                ),
                "first_recovering_fill_timestamp": (
                    None if first is None else first["fill_timestamp"]
                ),
                "first_recovering_fill_price": (
                    None if first is None else first["fill_price"]
                ),
                "recovered_at_6pct": any(
                    abs(float(r["minimum_start_distance_pct"]) - 0.06) < 1e-12
                    and r.get("recovered")
                    for r in mode_rows
                ),
            }
        )

    threshold_summary = []
    for thr in THRESHOLDS:
        row: dict[str, Any] = {"minimum_start_distance_pct": thr}
        for mode in TIMING_MODES:
            m = next(
                r
                for r in all_rows
                if r["timing_mode"] == mode
                and abs(float(r["minimum_start_distance_pct"]) - thr) < 1e-12
            )
            row[f"{mode}_recovered"] = m["recovered"]
            row[f"{mode}_fill_timestamp"] = m["fill_timestamp"]
            row[f"{mode}_fill_price"] = m["fill_price"]
            row[f"{mode}_exit_econ"] = m["final_total_exit_economics"]
        threshold_summary.append(row)

    integrity = {
        "decision": decision,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "baseline_fingerprint_ok": fp_ok,
        "baseline_fingerprint_failures": fp_base_fails,
        "winner_t0_6_fingerprint_ok": winner_ok,
        "winner_t0_6_fingerprint_failures": winner_fails,
        "no_low_fills": all(not r.get("used_low_as_fill") for r in all_rows),
        "tem_orders_imported": False,
        "initial_entry_created": False,
        "live_mode": "T0",
        "conservative_modes": ["T1", "T2"],
        "control_mode": "T3",
    }

    write_csv(output_dir / "all_variants.csv", all_rows)
    write_csv(output_dir / "timing_summary.csv", timing_summary)
    write_csv(output_dir / "threshold_summary.csv", threshold_summary)
    write_csv(output_dir / "trigger_events.csv", triggers)
    write_csv(output_dir / "fill_semantics_audit.csv", fill_audit)
    atomic_write_json(output_dir / "integrity.json", integrity)
    atomic_write_text(
        output_dir / "REPORT.md",
        build_report(
            decision=decision,
            all_rows=all_rows,
            timing_summary=timing_summary,
            integrity=integrity,
            winner=winner,
        ),
    )
    return {
        "decision": decision,
        "output_dir": str(output_dir),
        "all_rows": all_rows,
        "integrity": integrity,
        "winner": winner,
    }


def build_report(
    *,
    decision: str,
    all_rows: list[dict[str, Any]],
    timing_summary: list[dict[str, Any]],
    integrity: dict[str, Any],
    winner: dict[str, Any] | None,
) -> str:
    def row6(mode: str) -> dict[str, Any] | None:
        for r in all_rows:
            if (
                r["timing_mode"] == mode
                and r.get("minimum_start_distance_pct") is not None
                and abs(float(r["minimum_start_distance_pct"]) - 0.06) < 1e-12
            ):
                return r
        return None

    t0, t1, t2, t3 = row6("T0"), row6("T1"), row6("T2"), row6("T3")

    cons_first = None
    for thr in THRESHOLDS:
        ok = True
        fills = []
        for mode in ("T1", "T2"):
            r = next(
                x
                for x in all_rows
                if x["timing_mode"] == mode
                and abs(float(x["minimum_start_distance_pct"]) - thr) < 1e-12
            )
            fills.append(r)
            if not r.get("recovered"):
                ok = False
        if ok:
            cons_first = (thr, fills)
            break

    lines = [
        "# APT Start-Distance Execution Timing Audit",
        "",
        f"**Decision: `{decision}`**",
        "",
        f"Baseline fingerprint OK: **{integrity['baseline_fingerprint_ok']}**",
        f"T0 6% winner fingerprint OK: **{integrity['winner_t0_6_fingerprint_ok']}**",
        "",
        "## Answers",
        "",
        f"1. T0 @ 6% recovered: **{bool(t0 and t0['recovered'])}** "
        f"(fill=`{None if not t0 else t0['fill_timestamp']}` @ `{None if not t0 else t0['fill_price']}`)",
        f"2. T1 @ 6% recovered: **{bool(t1 and t1['recovered'])}** "
        f"(trigger=`{None if not t1 else t1['trigger_timestamp']}`, "
        f"fill=`{None if not t1 else t1['fill_timestamp']}` @ `{None if not t1 else t1['fill_price']}`)",
        f"3. T2 @ 6% recovered: **{bool(t2 and t2['recovered'])}** "
        f"(fill=`{None if not t2 else t2['fill_timestamp']}` @ `{None if not t2 else t2['fill_price']}`)",
        f"4. T3 @ 6% recovered: **{bool(t3 and t3['recovered'])}** "
        f"(fill=`{None if not t3 else t3['fill_timestamp']}` @ `{None if not t3 else t3['fill_price']}`)",
        "5. Planned live semantics: **T0** (observe current price; market fill immediately).",
        "6. Smallest threshold recovering under conservative T1∩T2: "
        + (
            f"**{cons_first[0]}** "
            f"(T1 fill `{cons_first[1][0]['fill_timestamp']}` @ `{cons_first[1][0]['fill_price']}`; "
            f"T2 fill `{cons_first[1][1]['fill_timestamp']}` @ `{cons_first[1][1]['fill_price']}`)"
            if cons_first
            else "**none**"
        ),
        "7. Trigger/fill delay impact:",
    ]
    for ts in timing_summary:
        lines.append(
            f"   - {ts['timing_mode']}: recovered={ts['n_recovered']}/7, "
            f"first_thr={ts['first_recovering_threshold']}, "
            f"at_6pct={ts['recovered_at_6pct']}"
        )
    lines.extend(
        [
            "8. Current APT open-based winner robustness: "
            + (
                "**same-open / live-path dependent**"
                if decision
                in (
                    "APT_START_DISTANCE_SAME_OPEN_DEPENDENT",
                    "APT_START_DISTANCE_LIVE_ONLY",
                )
                else (
                    "**causally robust across conservative modes**"
                    if decision == "APT_START_DISTANCE_EXECUTION_ROBUST"
                    else f"**see decision `{decision}`**"
                )
            ),
            "9. Rule for subsequent 25-blocker audit: "
            + (
                "Use `minimum_start_distance_pct` with **explicit timing mode**. "
                "Prefer conservative **T1/T2** (prior close → next/current open) if a "
                "recovering threshold exists; do not silently assume T0 same-open fills "
                "equal live latency. On APT, document T0@6% as the open-path reference "
                f"winner (fill={None if winner is None else winner.get('fill_timestamp')})."
            ),
            "",
            f"Decision: `{decision}`",
            "",
        ]
    )
    return "\n".join(lines) + "\n"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="APT start-distance execution timing audit (T0–T3)"
    )
    p.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    p.add_argument("--handoff-dir", type=Path, default=HANDOFF_DIR)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    out = run_audit(output_dir=args.output_dir, handoff_dir=args.handoff_dir)
    print(
        json.dumps(
            {
                "decision": out["decision"],
                "output_dir": out["output_dir"],
                "baseline_ok": out["integrity"]["baseline_fingerprint_ok"],
                "winner_ok": out["integrity"]["winner_t0_6_fingerprint_ok"],
            },
            indent=2,
        )
    )
    return 0 if "FAIL" not in out["decision"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

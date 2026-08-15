"""APT causal audit for start-distance and post-add-distance Cobertura guards."""

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
from .historical_blocker_state_extraction import compute_neutralization
from .runner import run_cobertura
from .start_distance import select_first_causal_start

HANDOFF_DIR = Path(
    "research/backtests/cobertura_0_notional_strategie/results/"
    "apt_cobertura_bundle_handoff_20260726"
)
DEFAULT_OUTPUT_DIR = Path(
    "research/backtests/cobertura_0_notional_strategie/results/"
    "apt_start_post_add_distance_audit_20260726"
)

SIGNAL_TS = "2026-01-19T00:00:00+00:00"
BREAK_LEVEL = 1.7639

START_GRID = [0.05, 0.06, 0.07, 0.08, 0.09, 0.10, 0.11, 0.12]
POST_ADD_GRID = [0.03, 0.04, 0.05, 0.06, 0.07]

FP_BASELINE = {
    "final_state": "DATA_END_OPEN",
    "bars_processed": 45945,
    "recovery_rounds": 16,
    "overlay_add_fills": 26,
    "overlay_be_closes": 16,
    "realized_overlay_pnl": 3.8645996,
    "final_total_exit_economics": -14.0576636,
}

STRATEGY = {
    "symbol": "APTUSDT",
    "timeframe": "5m",
    "direction_mode": "short_only",
    "activation_move_pct": 0.05,
    "first_add_move_pct": 0.06,
    "add_step_pct": 0.01,
    "add_size_pct": 0.4,
    "max_add_count": 8,
    "max_adds_per_candle": 4,
    "reset_reference_after_overlay_be": True,
    "max_overlay_qty_multiple": 4.0,
    "fee_rate_open": 0.00055,
    "fee_rate_close": 0.00055,
    "slippage_bps_open": 0.0,
    "slippage_bps_close": 0.0,
    "fee_buffer_usdt": 0.0,
    "overlay_exit_policy": "shared_be",
    "overlay_be_target_usdt": 0.0,
    "full_exit_target_mode": "legacy",
    "full_exit_target_usdt": 0.0,
    "target_total_pnl_usdt": 0.0,
    "target_profit_buffer_usdt": 0.0,
    "pnl_tolerance_usdt": 0.01,
    "candle_limit": 50_000,
    "start_price_source": "config_start_price",
    "end_timestamp": None,
}


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def load_pre_neutralization_book(handoff_dir: Path) -> dict[str, float]:
    before = _load_json(Path(handoff_dir) / "handoff_state_before_neutralization.json")
    pos = before["position"]
    lq = float(pos["long_qty"])
    sq = float(pos["short_qty"])
    return {
        "long_qty": lq,
        "long_avg": float(pos["long_avg"]),
        "short_qty": sq,
        "short_avg": float(pos["short_avg"]),
        "neutralization_qty": lq - sq,
        "structure_break_level": float(
            (before.get("trigger") or {}).get("structure_break_level") or BREAK_LEVEL
        ),
        "signal_available_ts": str(
            (before.get("trigger") or {}).get("signal_available_ts") or SIGNAL_TS
        ),
    }


def neutralize_at_price(book: dict[str, float], fill_price: float) -> dict[str, Any]:
    neut = compute_neutralization(
        long_qty=book["long_qty"],
        long_avg=book["long_avg"],
        short_qty=book["short_qty"],
        short_avg=book["short_avg"],
        fill_price=float(fill_price),
        taker_fee_rate=0.00055,
    )
    return {
        "core_long_qty": float(neut["post_neutralization_long_qty"]),
        "core_long_avg": float(neut["post_neutralization_long_avg"]),
        "core_short_qty": float(neut["post_neutralization_short_qty"]),
        "core_short_avg": float(neut["post_neutralization_short_avg"]),
        "neutralization": neut,
    }


def _approx(a: Any, b: Any, rel: float = 1e-6, abs_tol: float = 1e-6) -> bool:
    if isinstance(a, str) or isinstance(b, str):
        return a == b
    aa, bb = float(a), float(b)
    return abs(aa - bb) <= max(abs_tol, rel * max(abs(aa), abs(bb)))


def _round_stats(rounds: list[dict[str, Any]]) -> dict[str, Any]:
    adds = [int(r.get("adds") or 0) for r in rounds]
    if not adds:
        return {
            "rounds_with_1_add": 0,
            "rounds_with_2_adds": 0,
            "rounds_with_3_adds": 0,
            "rounds_with_4_or_more_adds": 0,
            "average_adds_per_round": 0.0,
            "max_adds_in_round": 0,
        }
    return {
        "rounds_with_1_add": sum(1 for a in adds if a == 1),
        "rounds_with_2_adds": sum(1 for a in adds if a == 2),
        "rounds_with_3_adds": sum(1 for a in adds if a == 3),
        "rounds_with_4_or_more_adds": sum(1 for a in adds if a >= 4),
        "average_adds_per_round": float(sum(adds)) / float(len(adds)),
        "max_adds_in_round": max(adds),
    }


def _risk_metrics(result: EngineResult) -> dict[str, Any]:
    fills = list(result.fills_events)
    trace = list(result.per_bar_trace)
    add_idxs = [
        i for i, f in enumerate(fills) if f.get("kind") == "overlay_short_add"
    ]
    be_ts = [
        str(f.get("timestamp"))
        for f in fills
        if f.get("kind") in ("overlay_be_close", "full_exit")
    ]

    max_rally = 0.0
    min_dist_after = None
    for i in add_idxs:
        f = fills[i]
        add_ts = _parse_ts(f["timestamp"])
        add_px = float(f["fill_price"])
        dist = f.get("projected_post_add_distance_pct")
        if dist is not None:
            d = float(dist)
            min_dist_after = d if min_dist_after is None else min(min_dist_after, d)
        # rally until next BE/full exit after this add
        end = None
        for t in be_ts:
            tt = _parse_ts(t)
            if tt >= add_ts:
                end = tt
                break
        peak = add_px
        for row in trace:
            rts = _parse_ts(row["timestamp"])
            if rts < add_ts:
                continue
            if end is not None and rts > end:
                break
            peak = max(peak, float(row["high"]))
        if add_px > 0:
            max_rally = max(max_rally, (peak - add_px) / add_px)

    max_ov_loss = 0.0
    max_gross = 0.0
    max_net_short = 0.0
    max_ov_qty = 0.0
    for row in trace:
        ov_pnl = float(row.get("overlay_open_pnl") or 0.0)
        max_ov_loss = min(max_ov_loss, ov_pnl)
        max_gross = max(max_gross, float(row.get("gross_notional") or 0.0))
        net = float(row.get("net_qty") or 0.0)
        max_net_short = max(max_net_short, max(0.0, -net))
        max_ov_qty = max(max_ov_qty, float(row.get("overlay_short_qty") or 0.0))

    return {
        "minimum_distance_to_total_short_avg_after_add": min_dist_after,
        "maximum_adverse_rally_before_overlay_close": max_rally,
        "maximum_overlay_unrealized_loss": abs(max_ov_loss) if max_ov_loss < 0 else 0.0,
        "maximum_total_gross_notional": max_gross,
        "maximum_net_short_qty": max_net_short,
        "max_overlay_qty": max_ov_qty,
    }


def metrics_for_variant(
    *,
    variant_id: str,
    start_sel: dict[str, Any],
    book: dict[str, float],
    min_start: float | None,
    min_post: float | None,
    policy: str,
    cfg: CoberturaConfig,
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
    guard = list(result.post_add_guard_events)
    scaled = [g for g in guard if g.get("action") == "scale_down"]
    skipped = [g for g in guard if g.get("action") == "skip"]
    dists = [
        float(g["projected_post_add_distance_pct"])
        for g in guard
        if g.get("projected_post_add_distance_pct") is not None
        and g.get("action") in ("fill", "scale_down")
    ]
    configured_total = sum(float(f.get("configured_qty") or f.get("qty") or 0) for f in adds)
    # Prefer configured from fills; fallback configured from guard events that filled
    if not any(f.get("configured_qty") is not None for f in adds):
        configured_total = sum(
            float(g.get("configured_candidate_add_qty") or 0)
            for g in guard
            if g.get("action") in ("fill", "scale_down")
        )
    else:
        configured_total = sum(float(f.get("configured_qty") or f["qty"]) for f in adds)
    actual_total = sum(float(f["qty"]) for f in adds)

    recovery_ts = None
    if result.state in ("RECOVERED", "RECOVERED_BE"):
        for row in reversed(result.per_bar_trace):
            if row.get("state") in ("RECOVERED", "RECOVERED_BE"):
                recovery_ts = row.get("timestamp")
                break

    risk = _risk_metrics(result)
    core_qty = float(cfg.core_long_qty)
    sel = start_sel["selected"]
    break_lvl = float(book["structure_break_level"])
    start_px = float(sel["price"])

    return {
        "variant_id": variant_id,
        "minimum_start_distance_pct": min_start,
        "minimum_post_add_distance_pct": min_post,
        "post_add_distance_policy": policy,
        "selected_start_timestamp": sel["timestamp"],
        "selected_start_price": start_px,
        "projected_short_avg_at_start": float(sel["projected_short_avg"]),
        "projected_start_distance_pct": float(sel["projected_start_distance_pct"]),
        "delay_bars_from_signal": int(sel["delay_bars_from_signal"]),
        "delay_minutes_from_signal": int(sel["delay_minutes_from_signal"]),
        "distance_from_break_level_pct": (start_px / break_lvl) - 1.0,
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
        "configured_add_qty_total": configured_total,
        "actual_add_qty_total": actual_total,
        "scaled_add_count": len(scaled),
        "skipped_add_count": len(skipped),
        "min_projected_post_add_distance_pct": min(dists) if dists else None,
        "average_projected_post_add_distance_pct": (
            sum(dists) / len(dists) if dists else None
        ),
        "max_overlay_qty": risk["max_overlay_qty"],
        "max_overlay_qty_multiple": (
            risk["max_overlay_qty"] / core_qty if core_qty > 0 else None
        ),
        "safety_violation_count": int(result.integrity.get("safety_violation_count") or 0),
        "no_negative_qty": bool(result.integrity.get("no_negative_qty")),
        "tranche_ledger_qty_sync": bool(result.integrity.get("tranche_ledger_qty_sync")),
        **_round_stats(result.overlay_rounds),
        **{k: v for k, v in risk.items() if k != "max_overlay_qty"},
    }


def build_cfg(
    *,
    variant_id: str,
    neut_book: dict[str, float],
    start_ts: str,
    start_price: float,
    min_start: float | None,
    min_post: float | None,
    policy: str,
    output_dir: Path | None,
) -> CoberturaConfig:
    raw: dict[str, Any] = {
        **STRATEGY,
        **neut_book,
        "start_timestamp": start_ts,
        "start_price": float(start_price),
        "minimum_start_distance_pct": min_start,
        "minimum_post_add_distance_pct": min_post,
        "post_add_distance_policy": policy,
        "output_dir": str(output_dir) if output_dir else None,
        "run_id": variant_id,
        "tags": {
            "audit": "start_post_add_distance",
            "variant_id": variant_id,
            "tem_orders_imported": False,
            "fresh_initial_entry_required": False,
        },
    }
    return CoberturaConfig.from_dict(raw)


def run_one_variant(
    *,
    variant_id: str,
    candles: list[dict[str, Any]],
    book: dict[str, float],
    min_start: float | None,
    min_post: float | None,
    policy: str,
    output_dir: Path | None,
    write_outputs: bool,
) -> tuple[dict[str, Any], EngineResult, dict[str, Any]]:
    start_sel = select_first_causal_start(
        candles,
        signal_ts=book["signal_available_ts"],
        existing_short_qty=book["short_qty"],
        existing_short_avg=book["short_avg"],
        neutralization_qty=book["neutralization_qty"],
        minimum_start_distance_pct=min_start,
        parse_ts=_parse_ts,
    )
    sel = start_sel["selected"]
    neut = neutralize_at_price(book, float(sel["price"]))
    cfg = build_cfg(
        variant_id=variant_id,
        neut_book={
            "core_long_qty": neut["core_long_qty"],
            "core_long_avg": neut["core_long_avg"],
            "core_short_qty": neut["core_short_qty"],
            "core_short_avg": neut["core_short_avg"],
        },
        start_ts=str(sel["timestamp"]),
        start_price=float(sel["price"]),
        min_start=min_start,
        min_post=min_post,
        policy=policy,
        output_dir=output_dir,
    )
    result = run_cobertura(cfg, candles=candles, write_outputs=write_outputs)
    metrics = metrics_for_variant(
        variant_id=variant_id,
        start_sel=start_sel,
        book=book,
        min_start=min_start,
        min_post=min_post,
        policy=policy,
        cfg=cfg,
        result=result,
    )
    return metrics, result, start_sel


def is_valid_candidate(m: dict[str, Any]) -> bool:
    if m.get("final_state") not in ("RECOVERED", "RECOVERED_BE"):
        return False
    econ = m.get("final_total_exit_economics")
    if econ is None or float(econ) < -float(STRATEGY["pnl_tolerance_usdt"]):
        return False
    if int(m.get("safety_violation_count") or 0) != 0:
        return False
    if not m.get("no_negative_qty"):
        return False
    if not m.get("tranche_ledger_qty_sync"):
        return False
    return True


def candidate_rank_key(m: dict[str, Any]) -> tuple:
    start_d = m.get("minimum_start_distance_pct")
    start_d_v = 999.0 if start_d is None else float(start_d)
    return (
        start_d_v,
        float(m.get("maximum_total_gross_notional") or 1e18),
        float(m.get("max_overlay_qty") or 1e18),
        int(m.get("bars_processed") or 10**9),
        -float(m.get("final_total_exit_economics") or -1e18),
    )


def decide(all_rows: list[dict[str, Any]], *, fp_ok: bool) -> str:
    if not fp_ok:
        return "APT_DISTANCE_GUARDS_FAIL"
    recovered = [r for r in all_rows if r.get("recovered")]
    if not recovered:
        return "APT_DISTANCE_GUARDS_NO_RECOVERY"

    start_only = [
        r
        for r in recovered
        if r.get("minimum_post_add_distance_pct") is None
        and r.get("post_add_distance_policy") == "disabled"
        and r.get("minimum_start_distance_pct") is not None
    ]
    post_only = [
        r
        for r in recovered
        if r.get("minimum_start_distance_pct") is None
        and r.get("minimum_post_add_distance_pct") is not None
        and not str(r.get("variant_id", "")).startswith("skip_control")
    ]
    combined = [
        r
        for r in recovered
        if r.get("minimum_start_distance_pct") is not None
        and r.get("minimum_post_add_distance_pct") is not None
        and r.get("post_add_distance_policy") == "scale_down"
    ]

    valids = [r for r in recovered if is_valid_candidate(r)]
    if not valids:
        return "APT_DISTANCE_GUARDS_UNSTABLE"

    if start_only and not post_only and not combined:
        return "APT_START_DISTANCE_ONLY_SUFFICIENT"
    if post_only and not start_only and not combined:
        return "APT_POST_ADD_DISTANCE_ONLY_SUFFICIENT"
    if start_only or combined or post_only:
        return "APT_DISTANCE_GUARDS_RECOVERY_FOUND"
    return "APT_DISTANCE_GUARDS_UNSTABLE"


def run_audit(
    *,
    output_dir: Path,
    handoff_dir: Path = HANDOFF_DIR,
    write_variant_artifacts: bool = False,
) -> dict[str, Any]:
    output_dir = Path(output_dir)
    if output_dir.exists() and (output_dir / "integrity.json").exists():
        raise FileExistsError(f"refusing to overwrite {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)
    for sub in (
        "baseline",
        "start_guard_only",
        "post_add_guard_only",
        "combined_grid",
        "best_scale_down",
        "best_skip_control",
    ):
        (output_dir / sub).mkdir(parents=True, exist_ok=True)

    book = load_pre_neutralization_book(handoff_dir)
    candles = load_candles_for_symbol(
        "APTUSDT", timeframe="5m", data_dir=DEFAULT_DATA_DIR, limit=50_000
    )

    all_rows: list[dict[str, Any]] = []
    start_events: list[dict[str, Any]] = []
    add_proj: list[dict[str, Any]] = []
    scaled_events: list[dict[str, Any]] = []
    skipped_events: list[dict[str, Any]] = []
    results_by_id: dict[str, EngineResult] = {}

    specs: list[tuple[str, float | None, float | None, str, Path]] = []
    specs.append(("baseline", None, None, "disabled", output_dir / "baseline"))
    for s in START_GRID:
        vid = f"start_{int(s*100):02d}pct"
        specs.append((vid, s, None, "disabled", output_dir / "start_guard_only" / vid))
    for p in POST_ADD_GRID:
        vid = f"post_{int(p*100):02d}pct"
        specs.append(
            (vid, None, p, "scale_down", output_dir / "post_add_guard_only" / vid)
        )
    for s in START_GRID:
        for p in POST_ADD_GRID:
            vid = f"start_{int(s*100):02d}__post_{int(p*100):02d}"
            specs.append(
                (
                    vid,
                    s,
                    p,
                    "scale_down",
                    output_dir / "combined_grid" / vid,
                )
            )

    for vid, s, p, policy, vdir in specs:
        vdir.mkdir(parents=True, exist_ok=True)
        metrics, result, start_sel = run_one_variant(
            variant_id=vid,
            candles=candles,
            book=book,
            min_start=s,
            min_post=p,
            policy=policy,
            output_dir=vdir if write_variant_artifacts else None,
            write_outputs=write_variant_artifacts,
        )
        all_rows.append(metrics)
        results_by_id[vid] = result
        atomic_write_json(vdir / "metrics.json", metrics)
        start_events.append(
            {
                "variant_id": vid,
                **start_sel["selected"],
                "minimum_start_distance_pct": s,
            }
        )
        for g in result.post_add_guard_events:
            row = {"variant_id": vid, **g}
            add_proj.append(row)
            if g.get("action") == "scale_down":
                scaled_events.append(row)
            if g.get("action") == "skip":
                skipped_events.append(row)

    baseline = next(r for r in all_rows if r["variant_id"] == "baseline")
    fp_fails = []
    for k, exp in FP_BASELINE.items():
        if not _approx(baseline.get(k), exp):
            fp_fails.append(f"{k}: got={baseline.get(k)} expected={exp}")
    fp_ok = not fp_fails

    scale_candidates = [
        r
        for r in all_rows
        if r["variant_id"] != "baseline"
        and r.get("post_add_distance_policy") == "scale_down"
        and is_valid_candidate(r)
    ]
    # Also allow start-only recovered with disabled post-add as candidates
    start_only_cands = [
        r
        for r in all_rows
        if r.get("minimum_start_distance_pct") is not None
        and r.get("minimum_post_add_distance_pct") is None
        and is_valid_candidate(r)
    ]
    candidates = scale_candidates + [
        c for c in start_only_cands if c not in scale_candidates
    ]
    best = min(candidates, key=candidate_rank_key) if candidates else None

    skip_control = None
    if best is not None and best.get("minimum_post_add_distance_pct") is not None:
        skip_vid = f"skip_control__{best['variant_id']}"
        metrics, result, start_sel = run_one_variant(
            variant_id=skip_vid,
            candles=candles,
            book=book,
            min_start=best.get("minimum_start_distance_pct"),
            min_post=best.get("minimum_post_add_distance_pct"),
            policy="skip",
            output_dir=output_dir / "best_skip_control",
            write_outputs=write_variant_artifacts,
        )
        skip_control = metrics
        all_rows.append(metrics)
        results_by_id[skip_vid] = result
        atomic_write_json(output_dir / "best_skip_control" / "metrics.json", metrics)
        for g in result.post_add_guard_events:
            row = {"variant_id": skip_vid, **g}
            add_proj.append(row)
            if g.get("action") == "skip":
                skipped_events.append(row)

    if best is not None:
        atomic_write_json(output_dir / "best_scale_down" / "metrics.json", best)
        best_result = results_by_id[best["variant_id"]]
        write_csv(
            output_dir / "best_variant_timeline.csv",
            best_result.per_bar_trace,
        )

    decision = decide(all_rows, fp_ok=fp_ok)

    start_only_rows = [
        r
        for r in all_rows
        if r.get("minimum_start_distance_pct") is not None
        and r.get("minimum_post_add_distance_pct") is None
        and r["variant_id"] != "baseline"
    ]
    post_only_rows = [
        r
        for r in all_rows
        if r.get("minimum_start_distance_pct") is None
        and r.get("minimum_post_add_distance_pct") is not None
        and not str(r["variant_id"]).startswith("skip_control")
    ]
    combined_rows = [
        r
        for r in all_rows
        if r.get("minimum_start_distance_pct") is not None
        and r.get("minimum_post_add_distance_pct") is not None
        and r.get("post_add_distance_policy") == "scale_down"
    ]

    integrity = {
        "decision": decision,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "baseline_fingerprint_ok": fp_ok,
        "baseline_fingerprint_failures": fp_fails,
        "variant_count": len(all_rows),
        "valid_candidate_count": len(candidates),
        "best_variant_id": None if best is None else best["variant_id"],
        "skip_control_variant_id": None if skip_control is None else skip_control["variant_id"],
        "book": book,
        "hypothesis_8_to_10_start": _hyp_check(start_only_rows),
        "tem_orders_imported": False,
        "initial_entry_created": False,
    }

    write_csv(output_dir / "all_variants.csv", all_rows)
    write_csv(output_dir / "start_guard_summary.csv", start_only_rows)
    write_csv(output_dir / "post_add_guard_summary.csv", post_only_rows)
    write_csv(output_dir / "combined_grid_summary.csv", combined_rows)
    write_csv(output_dir / "start_trigger_events.csv", start_events)
    write_csv(output_dir / "add_projection_events.csv", add_proj)
    write_csv(output_dir / "scaled_add_events.csv", scaled_events)
    write_csv(output_dir / "skipped_add_events.csv", skipped_events)
    atomic_write_json(output_dir / "integrity.json", integrity)
    atomic_write_text(
        output_dir / "REPORT.md",
        build_report(
            decision=decision,
            baseline=baseline,
            start_only_rows=start_only_rows,
            post_only_rows=post_only_rows,
            combined_rows=combined_rows,
            best=best,
            skip_control=skip_control,
            integrity=integrity,
            fp_ok=fp_ok,
        ),
    )
    return {
        "decision": decision,
        "output_dir": str(output_dir),
        "all_rows": all_rows,
        "best": best,
        "skip_control": skip_control,
        "integrity": integrity,
        "baseline": baseline,
    }


def _hyp_check(start_only_rows: list[dict[str, Any]]) -> dict[str, Any]:
    recovered_starts = sorted(
        float(r["minimum_start_distance_pct"])
        for r in start_only_rows
        if r.get("recovered") and r.get("minimum_start_distance_pct") is not None
    )
    threshold = recovered_starts[0] if recovered_starts else None
    return {
        "first_recovering_start_distance_pct": threshold,
        "in_8_to_10_band": (
            threshold is not None and 0.08 - 1e-12 <= threshold <= 0.10 + 1e-12
        ),
        "all_recovering_start_distances": recovered_starts,
    }


def build_report(
    *,
    decision: str,
    baseline: dict[str, Any],
    start_only_rows: list[dict[str, Any]],
    post_only_rows: list[dict[str, Any]],
    combined_rows: list[dict[str, Any]],
    best: dict[str, Any] | None,
    skip_control: dict[str, Any] | None,
    integrity: dict[str, Any],
    fp_ok: bool,
) -> str:
    start_rec = [r for r in start_only_rows if r.get("recovered")]
    post_rec = [r for r in post_only_rows if r.get("recovered")]
    comb_rec = [r for r in combined_rows if r.get("recovered")]
    first_start = None
    if start_rec:
        first_start = min(start_rec, key=lambda r: float(r["minimum_start_distance_pct"]))

    lines = [
        "# APT Start / Post-Add Distance Guard Audit",
        "",
        f"**Decision: `{decision}`**",
        "",
        f"Baseline fingerprint OK: **{fp_ok}**",
        "",
        "## Answers",
        "",
        f"1. First recovering start distance: "
        f"**{None if first_start is None else first_start['minimum_start_distance_pct']}** "
        f"(`{None if first_start is None else first_start['selected_start_timestamp']}` @ "
        f"`{None if first_start is None else first_start['selected_start_price']}`)",
        "2. Start candles by threshold:",
    ]
    for r in sorted(start_only_rows, key=lambda x: float(x["minimum_start_distance_pct"])):
        lines.append(
            f"   - {float(r['minimum_start_distance_pct'])*100:.0f}% → "
            f"`{r['selected_start_timestamp']}` @ `{r['selected_start_price']}` "
            f"(dist={r['projected_start_distance_pct']:.4f}, recovered={r['recovered']})"
        )
    hyp = integrity.get("hypothesis_8_to_10_start") or {}
    lines.extend(
        [
            f"3. Hypothesis 8–10% band: **{hyp.get('in_8_to_10_band')}** "
            f"(first={hyp.get('first_recovering_start_distance_pct')})",
            f"4. Start-guard alone sufficient: **{bool(start_rec)}** "
            f"(n_recovered={len(start_rec)})",
            f"5. Post-add-guard alone sufficient: **{bool(post_rec)}** "
            f"(n_recovered={len(post_rec)})",
            f"6. Combination improves: **{len(comb_rec) > 0 and not start_rec}** "
            f"(combined_recovered={len(comb_rec)}, start_only={len(start_rec)}; "
            "note: many combined hits reuse the same start-only recovery path)",
            "7. Best post-add rally buffer among recovered combined/post-only:",
        ]
    )
    pool = [r for r in (combined_rows + post_only_rows) if r.get("recovered")]
    if pool:
        best_buf = max(
            pool, key=lambda r: float(r.get("maximum_adverse_rally_before_overlay_close") or 0)
        )
        lines.append(
            f"   - `{best_buf['variant_id']}` rally="
            f"`{best_buf.get('maximum_adverse_rally_before_overlay_close')}` "
            f"min_post_dist=`{best_buf.get('min_projected_post_add_distance_pct')}`"
        )
    else:
        lines.append("   - none recovered")

    if best is None:
        lines.append("8. Scaled/skipped adds (best): n/a")
        lines.append("9. Overlay/capital reduction: n/a")
        lines.append("10. Early BE prevention: n/a")
        lines.append("11. Earliest robust recovery variant: **none**")
        lines.append("12. scale_down vs skip: n/a")
    else:
        lines.extend(
            [
                f"8. Best `{best['variant_id']}` scaled=`{best['scaled_add_count']}` "
                f"skipped=`{best['skipped_add_count']}`",
                f"9. Best max_overlay_qty=`{best['max_overlay_qty']}` "
                f"max_gross=`{best['maximum_total_gross_notional']}` "
                f"(baseline overlay={baseline.get('max_overlay_qty')})",
                f"10. Best overlay_be_closes=`{best['overlay_be_closes']}` vs "
                f"baseline `{baseline['overlay_be_closes']}`",
                f"11. Earliest robust candidate: **`{best['variant_id']}`** "
                f"start=`{best['selected_start_timestamp']}` @ `{best['selected_start_price']}` "
                f"proj_avg=`{best['projected_short_avg_at_start']}` "
                f"econ=`{best['final_total_exit_economics']}` "
                f"bars=`{best['bars_processed']}`",
                "12. scale_down vs skip: "
                + (
                    f"scale_down recovered=`{best['recovered']}` econ=`{best['final_total_exit_economics']}`; "
                    f"skip recovered=`{skip_control.get('recovered')}` "
                    f"econ=`{skip_control.get('final_total_exit_economics')}`"
                    if skip_control
                    else "no skip control (best had no post-add guard)"
                ),
            ]
        )
    lines.extend(
        [
            "13. Transfer recommendation: "
            + (
                "Prefer start-distance guard first; add post-add scale_down only if "
                "start-only remains fragile on capital/overlay."
                if decision
                in (
                    "APT_DISTANCE_GUARDS_RECOVERY_FOUND",
                    "APT_START_DISTANCE_ONLY_SUFFICIENT",
                )
                else "Do not transfer yet; guards did not yield a stable recovery rule."
            ),
            "",
            f"Decision: `{decision}`",
            "",
        ]
    )
    return "\n".join(lines) + "\n"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="APT start/post-add distance guard audit")
    p.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    p.add_argument("--handoff-dir", type=Path, default=HANDOFF_DIR)
    p.add_argument(
        "--write-variant-artifacts",
        action="store_true",
        help="Write full Cobertura artifacts per variant (slow/large)",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    out = run_audit(
        output_dir=args.output_dir,
        handoff_dir=args.handoff_dir,
        write_variant_artifacts=args.write_variant_artifacts,
    )
    print(
        json.dumps(
            {
                "decision": out["decision"],
                "output_dir": out["output_dir"],
                "baseline_ok": out["integrity"]["baseline_fingerprint_ok"],
                "best_variant_id": out["integrity"]["best_variant_id"],
                "hypothesis": out["integrity"]["hypothesis_8_to_10_start"],
            },
            indent=2,
        )
    )
    return 0 if out["decision"] != "APT_DISTANCE_GUARDS_FAIL" else 1


if __name__ == "__main__":
    raise SystemExit(main())

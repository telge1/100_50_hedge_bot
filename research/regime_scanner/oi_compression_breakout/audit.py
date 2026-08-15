"""Orchestrate OI compression breakout event audit (read-only)."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import pandas as pd

from research.regime_scanner.oi_compression_breakout.boxes import (
    boxes_to_rows,
    detect_frozen_boxes_with_early_release,
    physical_phases_from_boxes,
)
from research.regime_scanner.oi_compression_breakout.config import OICBConfig, default_config
from research.regime_scanner.oi_compression_breakout.controls import sample_controls
from research.regime_scanner.oi_compression_breakout.features import enrich_symbol_frame
from research.regime_scanner.oi_compression_breakout.loader import coverage_report, load_frames
from research.regime_scanner.oi_compression_breakout.oi_groups import assign_oi_groups, compute_oi_features
from research.regime_scanner.oi_compression_breakout.outcomes import compute_breakout_outcomes

logger = logging.getLogger(__name__)


def _bars_to_breakout_stats(breakouts: list[dict[str, Any]]) -> dict[str, Any]:
    vals = [
        int(b["bars_to_breakout"])
        for b in breakouts
        if not b.get("no_breakout") and b.get("bars_to_breakout") is not None
    ]
    if not vals:
        return {"n": 0, "median": None, "p90": None, "max": None, "share_1_bar": None, "mean": None}
    s = pd.Series(vals)
    return {
        "n": int(len(vals)),
        "share_1_bar": float((s == 1).mean()),
        "median": float(s.median()),
        "p75": float(s.quantile(0.75)),
        "p90": float(s.quantile(0.90)),
        "mean": float(s.mean()),
        "max": int(s.max()),
        "min": int(s.min()),
    }


def population_counters(breakouts: list[dict[str, Any]], diag_rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate confirm / breakout / window counters for summary logs."""
    raw = int(sum(int(r.get("raw_candidates") or 0) for r in diag_rows))
    confirmed = len(breakouts)
    with_bo = sum(1 for b in breakouts if not b.get("no_breakout"))
    without = sum(1 for b in breakouts if b.get("no_breakout"))

    def _status(*names: str) -> int:
        return sum(1 for b in breakouts if b.get("outcome_status") in names or b.get("status") in names)

    w_counts = {}
    for w in (3, 6, 12, 24, 48):
        w_counts[f"W{w}_breakouts"] = sum(1 for b in breakouts if b.get(f"W{w}_any"))

    bars = _bars_to_breakout_stats(breakouts)
    return {
        "raw_box_candidates": raw,
        "confirmed_boxes": confirmed,
        "boxes_with_breakout": with_bo,
        "boxes_without_breakout": without,
        "boxes_gap_aborted": _status("gap_abort"),
        "boxes_sequence_ended": _status("sequence_end"),
        "boxes_dataset_ended": _status("dataset_end"),
        "boxes_invalidated": sum(1 for b in breakouts if b.get("invalidated")),
        "boxes_timeout": _status("no_breakout_timeout", "timeout_no_breakout"),
        **w_counts,
        "bars_to_breakout_median": bars.get("median"),
        "bars_to_breakout_p90": bars.get("p90"),
        "bars_to_breakout_max": bars.get("max"),
        "search_horizon_bars_mode": int(
            pd.Series([b.get("search_horizon_bars") for b in breakouts if b.get("search_horizon_bars") is not None]).mode().iloc[0]
        )
        if any(b.get("search_horizon_bars") is not None for b in breakouts)
        else None,
    }


def build_candidate_tables(
    *,
    boxes: list[Any],
    oi_rows: list[dict[str, Any]],
    breakouts: list[dict[str, Any]],
    box_by_id: dict[str, Any],
    df: pd.DataFrame,
    cfg: OICBConfig,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Merge OI candidates with shared box-level breakout results."""
    br_by_box = {b["box_id"]: b for b in breakouts}
    candidate_breakouts: list[dict[str, Any]] = []
    candidate_forwards: list[dict[str, Any]] = []
    box_forwards: list[dict[str, Any]] = []  # one per box with fill (legacy/compat)

    computed_fwd: dict[str, dict[str, Any]] = {}

    for oi in oi_rows:
        box_id = oi["box_id"]
        br = br_by_box.get(box_id)
        if br is None:
            continue
        cand = {
            "candidate_id": oi["candidate_id"],
            "box_id": box_id,
            "physical_id": oi.get("physical_id"),
            "symbol": oi.get("symbol"),
            "box_length": oi.get("box_length"),
            "quality": oi.get("quality"),
            "oi_group": oi.get("oi_group"),
            "is_parent_O0": oi.get("is_parent_O0"),
            "oi_change_pct": oi.get("oi_change_pct"),
            "insufficient_warmup": oi.get("insufficient_warmup"),
            "no_breakout": br.get("no_breakout"),
            "status": br.get("status"),
            "invalidated": br.get("invalidated"),
            "invalidation_reason": br.get("invalidation_reason"),
            "breakout_side": br.get("breakout_side"),
            "breakout_i": br.get("breakout_i"),
            "bars_to_breakout": br.get("bars_to_breakout"),
            "fill_i": br.get("fill_i"),
            "fill_price": br.get("fill_price"),
            "fill_bucket": br.get("fill_bucket"),
            "breakout_bucket": br.get("breakout_bucket"),
        }
        for w in (3, 6, 12, 24, 48):
            for suf in ("any", "none", "long", "short", "both"):
                k = f"W{w}_{suf}"
                if k in br:
                    cand[k] = br[k]
        candidate_breakouts.append(cand)

        if br.get("no_breakout") or br.get("fill_i") is None or br.get("breakout_side") is None:
            continue

        if box_id not in computed_fwd:
            b = box_by_id[box_id]
            oc = compute_breakout_outcomes(
                df,
                fill_i=int(br["fill_i"]),
                entry=float(br["fill_price"]),
                side=str(br["breakout_side"]),
                box_width=float(b.box_width),
                box_high=float(b.box_high),
                box_low=float(b.box_low),
                breakout_i=int(br["breakout_i"]),
                compute_exits=cfg.compute_exits,
            )
            computed_fwd[box_id] = {
                "box_id": box_id,
                "physical_id": b.physical_id,
                "symbol": b.symbol,
                "box_length": b.box_length,
                "quality": b.quality,
                "breakout_side": br["breakout_side"],
                "breakout_bucket": br.get("breakout_bucket"),
                "fill_bucket": br.get("fill_bucket"),
                "fill_price": br.get("fill_price"),
                "bars_to_breakout": br.get("bars_to_breakout"),
                "oi_change_pct": br.get("oi_change_pct"),
                **oc,
            }
            box_forwards.append(computed_fwd[box_id])

        base = computed_fwd[box_id]
        candidate_forwards.append(
            {
                "candidate_id": oi["candidate_id"],
                "oi_group": oi["oi_group"],
                "is_parent_O0": oi.get("is_parent_O0"),
                **base,
            }
        )

    return candidate_breakouts, candidate_forwards, box_forwards


def run_symbol(df: pd.DataFrame, cfg: OICBConfig) -> dict[str, Any]:
    df = enrich_symbol_frame(df)
    boxes, breakouts, diag = detect_frozen_boxes_with_early_release(
        df, max_wait_bars=cfg.max_wait_bars
    )
    oi_feats = [compute_oi_features(df, b) for b in boxes]
    oi_rows = assign_oi_groups(boxes, oi_feats, min_history=cfg.oi_pctl_min_history)
    feat_by_id = {b.box_id: f for b, f in zip(boxes, oi_feats)}
    box_by_id = {b.box_id: b for b in boxes}

    # attach oi_change onto breakout rows
    for br in breakouts:
        br["oi_change_pct"] = feat_by_id.get(br["box_id"], {}).get("oi_change_pct")

    cand_br, cand_fwd, box_fwd = build_candidate_tables(
        boxes=boxes,
        oi_rows=oi_rows,
        breakouts=breakouts,
        box_by_id=box_by_id,
        df=df,
        cfg=cfg,
    )

    controls = sample_controls(df, boxes, feat_by_id)
    for br in breakouts:
        feat = feat_by_id.get(br["box_id"]) or {}
        chg = feat.get("oi_change_pct")
        if chg is not None and chg > 0 and br.get("no_breakout"):
            controls.append(
                {
                    "control": "C2",
                    "box_id": br["box_id"],
                    "symbol": br["symbol"],
                    "bucket_start": br.get("breakout_bucket") or br.get("fill_bucket") or "",
                    "oi_change_pct": chg,
                    "note": "oi_buildup_no_breakout_in_max_wait",
                }
            )

    n_with = sum(1 for b in breakouts if not b.get("no_breakout"))
    n_without = sum(1 for b in breakouts if b.get("no_breakout"))
    pop = population_counters(breakouts, diag.to_rows(str(df["symbol"].iloc[0]) if len(df) else ""))
    return {
        "boxes": boxes_to_rows(boxes),
        "physical_phases": physical_phases_from_boxes(boxes),
        "oi_features": oi_rows,
        "breakouts": breakouts,
        "outcomes": box_fwd,
        "candidate_breakout_outcomes": cand_br,
        "candidate_forward_outcomes": cand_fwd,
        "controls": controls,
        "filter_diagnostics": diag.to_rows(str(df["symbol"].iloc[0]) if len(df) else ""),
        "n_boxes": len(boxes),
        "n_breakouts": n_with,
        "n_no_breakout": n_without,
        "bars_to_breakout_stats": _bars_to_breakout_stats(breakouts),
        "population_counters": pop,
    }


def run_audit(
    *,
    symbols: list[str],
    start: datetime,
    end: datetime,
    import_version: str,
    cfg: OICBConfig | None = None,
) -> dict[str, Any]:
    cfg = cfg or default_config()
    joined, frames, cov = load_frames(
        symbols=symbols, start=start, end=end, import_version=import_version
    )
    all_boxes: list[dict[str, Any]] = []
    all_phases: list[dict[str, Any]] = []
    all_oi: list[dict[str, Any]] = []
    all_br: list[dict[str, Any]] = []
    all_oc: list[dict[str, Any]] = []
    all_cand_br: list[dict[str, Any]] = []
    all_cand_fwd: list[dict[str, Any]] = []
    all_ctrl: list[dict[str, Any]] = []
    all_diag: list[dict[str, Any]] = []
    by_symbol: dict[str, Any] = {}
    pop_acc: list[dict[str, Any]] = []

    for sym in symbols:
        df = frames.get(sym)
        if df is None or df.empty:
            by_symbol[sym] = {"joined_rows": 0, "boxes": 0, "breakouts": 0, "no_breakout": 0}
            continue
        logger.info("symbol=%s rows=%s", sym, len(df))
        res = run_symbol(df, cfg)
        all_boxes.extend(res["boxes"])
        all_phases.extend(res["physical_phases"])
        all_oi.extend(res["oi_features"])
        all_br.extend(res["breakouts"])
        all_oc.extend(res["outcomes"])
        all_cand_br.extend(res["candidate_breakout_outcomes"])
        all_cand_fwd.extend(res["candidate_forward_outcomes"])
        all_ctrl.extend(res["controls"])
        all_diag.extend(res["filter_diagnostics"])
        pop_acc.append(res["population_counters"])
        by_symbol[sym] = {
            "joined_rows": int(len(df)),
            "boxes": int(res["n_boxes"]),
            "breakouts": int(res["n_breakouts"]),
            "no_breakout": int(res["n_no_breakout"]),
            "outcomes": int(len(res["outcomes"])),
            "candidate_breakouts": int(len(res["candidate_breakout_outcomes"])),
            "bars_to_breakout_stats": res["bars_to_breakout_stats"],
            "population_counters": res["population_counters"],
        }

    oi_dist: dict[str, int] = {}
    for r in all_oi:
        g = r.get("oi_group")
        oi_dist[str(g)] = oi_dist.get(str(g), 0) + 1

    length_dist: dict[str, int] = {}
    for r in all_boxes:
        k = str(r.get("box_length"))
        length_dist[k] = length_dist.get(k, 0) + 1

    pop = population_counters(all_br, all_diag)

    return {
        "coverage": cov if cov else coverage_report(joined),
        "joined_rows": int(len(joined)),
        "boxes": all_boxes,
        "physical_phases": all_phases,
        "oi_features": all_oi,
        "breakouts": all_br,
        "outcomes": all_oc,
        "candidate_breakout_outcomes": all_cand_br,
        "candidate_forward_outcomes": all_cand_fwd,
        "controls": all_ctrl,
        "filter_diagnostics": all_diag,
        "by_symbol": by_symbol,
        "oi_group_counts": oi_dist,
        "box_length_counts": length_dist,
        "n_boxes_with_breakout": sum(1 for b in all_br if not b.get("no_breakout")),
        "n_boxes_without_breakout": sum(1 for b in all_br if b.get("no_breakout")),
        "bars_to_breakout_stats": _bars_to_breakout_stats(all_br),
        "population_counters": pop,
        "config_hash": cfg.config_hash(),
        "db_writes": False,
        "max_wait_bars": cfg.max_wait_bars,
    }

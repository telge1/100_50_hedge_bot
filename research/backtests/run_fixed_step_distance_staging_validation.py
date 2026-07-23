#!/usr/bin/env python3
"""Fixed-step distance staging validation (research-only).

Profiles (default bucket_coverage set):
  legacy, two_early_medium, adaptive_equal,
  fixed_step_1pct_equal, fixed_step_2pct_equal, fixed_step_2pct_backloaded

Does NOT auto-start a 27-coin full run. Use --print-manual-commands-only.
Never overwrite protected prior result directories.
"""

from __future__ import annotations

import argparse
import csv
import json
import time
import traceback
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from research.backtests.adaptive_distance_staging_metrics import (
    blocker_summary_by_profile,
    compare_profiles,
    summarize_by_distance_bucket,
    summarize_by_profile_distance_bucket,
)
from research.backtests.candle_loader import load_candles_for_symbol
from research.backtests.historical_backtest import normalize_candles
from research.backtests.inventory_mtm_freeze import safe_float
from research.backtests.multicoin_blocker_price_staging import (
    DEFAULT_BASELINE,
    FULL_HISTORY_CANDLE_LIMIT,
)
from research.backtests.multicoin_price_staging_grid import (
    atomic_write_json,
    assert_output_dir_safe,
    load_checkpoint,
    load_csv_rows,
    write_csv,
)
from research.backtests.run_two_early_medium_large_multicoin_window_validation import (
    plan_universe_and_starts,
)
from research.backtests.run_two_early_medium_multistart_validation import run_profile_at_start
from research.backtests.two_early_medium_multistart_metrics import summarize_pairs
from research.backtests.two_early_medium_multistart_starts import DEFAULT_SEED, DEFAULT_WARMUP
from research.backtests.two_early_medium_window_metrics import summarize_by_keys
from research.backtests.two_early_medium_window_plan import (
    TARGET_STARTS_PER_WINDOW,
    window_profile_run_key,
)

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUT = (
    ROOT / "research/backtests/results/fixed_step_distance_staging_validation_1000_500_20260722"
)
DEFAULT_LOG_DIR = ROOT / "research/backtests/results/_logs"
DEFAULT_BUCKET_STARTS = (
    ROOT / "research/backtests/results/adaptive_distance_bucket_candidates_20260722/selected_starts.csv"
)
DEFAULT_PROFILES = (
    "legacy",
    "two_early_medium",
    "adaptive_equal",
    "fixed_step_1pct_equal",
    "fixed_step_2pct_equal",
    "fixed_step_2pct_backloaded",
)
CANDIDATE_PROFILES_DEFAULT = tuple(p for p in DEFAULT_PROFILES if p != "legacy")
PROTECTED = (
    ROOT / "research/backtests/results/two_early_medium_large_multicoin_window_validation_1000_500_20260721",
    ROOT / "research/backtests/results/adaptive_distance_staging_stage_c_20260722",
    ROOT / "research/backtests/results/adaptive_distance_bucket_candidates_20260722",
    ROOT / "research/backtests/results/adaptive_distance_bucket_coverage_20260722",
    ROOT / "research/backtests/results/adaptive_distance_bucket_unknown_audit_20260722",
    DEFAULT_BASELINE,
)
STAGE_C_COINS = (
    "APTUSDT",
    "BTCUSDT",
    "ETHUSDT",
    "BCHUSDT",
    "OPUSDT",
    "ATOMUSDT",
    "TRXUSDT",
)


def log(msg: str) -> None:
    print(msg, flush=True)


def _append_csv_row(path: Path, row: dict[str, Any], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = path.exists() and path.stat().st_size > 0
    with path.open("a", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        if not exists:
            w.writeheader()
        w.writerow(
            {
                k: (json.dumps(v, default=str) if isinstance(v, (list, dict)) else v)
                for k, v in row.items()
                if k in fieldnames
            }
        )


def _empty_checkpoint(*, coins: list[str], planned_pairs: int, profiles: Sequence[str]) -> dict[str, Any]:
    return {
        "version": 1,
        "profiles": list(profiles),
        "coins": list(coins),
        "planned_pairs": planned_pairs,
        "completed_pair_keys": [],
        "completed_run_keys": [],
        "errors": [],
        "updated_at": None,
    }


def _lost_additional_rows(pairs: list[dict[str, Any]], *, kind: str) -> list[dict[str, Any]]:
    out = []
    for p in pairs:
        lost = int(p.get("legacy_valid_close") or 0) == 1 and int(p.get("staging_valid_close") or 0) == 0
        add = int(p.get("legacy_valid_close") or 0) == 0 and int(p.get("staging_valid_close") or 0) == 1
        if kind == "lost" and lost:
            out.append(p)
        if kind == "additional" and add:
            out.append(p)
    return out


def _summarize_by_grid_step(raw_rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in raw_rows:
        if str(r.get("profile") or "") == "legacy":
            continue
        key = str(r.get("grid_step_pct") or r.get("profile") or "none")
        groups[key].append(r)
    rows = []
    for key, subset in sorted(groups.items()):
        rows.append(
            {
                "grid_step_or_profile": key,
                "n_runs": len(subset),
                "staging_activated": sum(int(safe_float(r.get("staging_activated"))) for r in subset),
                "mean_requested_stages": (
                    sum(safe_float(r.get("requested_stage_count")) for r in subset) / len(subset)
                ),
                "mean_effective_stages": (
                    sum(safe_float(r.get("effective_stage_count_after_rounding")) for r in subset)
                    / len(subset)
                ),
                "sum_skipped_small_stages": sum(
                    int(safe_float(r.get("skipped_small_stages"))) for r in subset
                ),
                "n_cap_applied": sum(int(safe_float(r.get("stage_cap_applied"))) for r in subset),
                "sum_total_pnl": sum(safe_float(r.get("total_pnl")) for r in subset),
            }
        )
    return rows


def _summarize_by_stage_count(
    raw_rows: Sequence[dict[str, Any]], *, field: str
) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in raw_rows:
        if str(r.get("profile") or "") == "legacy":
            continue
        groups[str(r.get(field) if r.get(field) not in (None, "") else "none")].append(r)
    out = []
    for key, subset in sorted(groups.items(), key=lambda kv: kv[0]):
        out.append(
            {
                field: key,
                "n_runs": len(subset),
                "staging_activated": sum(int(safe_float(r.get("staging_activated"))) for r in subset),
                "sum_total_pnl": sum(safe_float(r.get("total_pnl")) for r in subset),
                "mean_fees": (
                    sum(safe_float(r.get("fees") or r.get("total_fees")) for r in subset) / len(subset)
                ),
            }
        )
    return out


def _stage_fill_analysis(raw_rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for r in raw_rows:
        if str(r.get("profile") or "") == "legacy":
            continue
        out.append(
            {
                "pair_key": r.get("pair_key"),
                "profile": r.get("profile"),
                "distance_bucket": r.get("theoretical_distance_bucket") or r.get("distance_bucket"),
                "grid_step_pct": r.get("grid_step_pct"),
                "requested_stage_count": r.get("requested_stage_count"),
                "capped_stage_count": r.get("capped_stage_count"),
                "effective_stage_count_after_rounding": r.get("effective_stage_count_after_rounding"),
                "stage_fill_count": r.get("stage_fill_count") or r.get("stage_activation_count"),
                "filled_stage_indices": r.get("filled_stage_indices"),
                "unfilled_stage_indices": r.get("unfilled_stage_indices"),
                "fallback_used": r.get("fallback_used"),
                "skipped_small_stages": r.get("skipped_small_stages"),
                "stage_cap_applied": r.get("stage_cap_applied"),
            }
        )
    return out


def _min_notional_fallbacks(raw_rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        r
        for r in raw_rows
        if str(r.get("fallback_used") or "") == "reduce_stage_count"
        and str(r.get("profile") or "") != "legacy"
    ]


def _fee_analysis(raw_rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    by_prof: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in raw_rows:
        by_prof[str(r.get("profile") or "")].append(r)
    out = []
    for prof, subset in sorted(by_prof.items()):
        out.append(
            {
                "profile": prof,
                "n": len(subset),
                "sum_fees": sum(safe_float(r.get("fees") or r.get("total_fees")) for r in subset),
                "mean_fees": (
                    sum(safe_float(r.get("fees") or r.get("total_fees")) for r in subset) / len(subset)
                ),
            }
        )
    return out


def _worst_cases(pairs: Sequence[dict[str, Any]], *, n: int = 15) -> list[dict[str, Any]]:
    ranked = sorted(pairs, key=lambda p: safe_float(p.get("delta_total_pnl")))
    return list(ranked[:n])


def build_artifacts(
    *,
    output_dir: Path,
    included: list[dict[str, Any]],
    excluded: list[dict[str, Any]],
    window_rows: list[dict[str, Any]],
    start_rows: list[dict[str, Any]],
    raw_rows: list[dict[str, Any]],
    manifest: dict[str, Any],
    profiles: Sequence[str],
) -> dict[str, Any]:
    start_meta = {str(r["pair_key"]): dict(r) for r in start_rows}
    by_key: dict[str, dict[str, dict[str, Any]]] = {}
    for row in raw_rows:
        by_key.setdefault(str(row.get("pair_key")), {})[str(row["profile"])] = row

    candidates = [p for p in profiles if p != "legacy"]
    pair_sets: dict[str, list[dict[str, Any]]] = {}
    for cand in candidates:
        pair_sets[f"{cand}_vs_legacy"] = []
        if "two_early_medium" in profiles and cand != "two_early_medium":
            pair_sets[f"{cand}_vs_two_early_medium"] = []
        if "adaptive_equal" in profiles and cand != "adaptive_equal":
            pair_sets[f"{cand}_vs_adaptive_equal"] = []

    for pk, profs in sorted(by_key.items()):
        meta = start_meta.get(pk, {"pair_key": pk})
        if "legacy" not in profs:
            continue
        for cand in candidates:
            if cand not in profs:
                continue
            pair_sets[f"{cand}_vs_legacy"].append(
                compare_profiles(
                    profs["legacy"],
                    profs[cand],
                    meta,
                    baseline_name="legacy",
                    candidate_name=cand,
                )
            )
            if "two_early_medium" in profs and cand != "two_early_medium":
                pair_sets[f"{cand}_vs_two_early_medium"].append(
                    compare_profiles(
                        profs["two_early_medium"],
                        profs[cand],
                        meta,
                        baseline_name="two_early_medium",
                        candidate_name=cand,
                    )
                )
            if "adaptive_equal" in profs and cand != "adaptive_equal":
                pair_sets[f"{cand}_vs_adaptive_equal"].append(
                    compare_profiles(
                        profs["adaptive_equal"],
                        profs[cand],
                        meta,
                        baseline_name="adaptive_equal",
                        candidate_name=cand,
                    )
                )

    # Primary ranking: fixed-step profiles vs TEM when present, else vs legacy
    primary_name = None
    for prefer in (
        "fixed_step_1pct_equal_vs_two_early_medium",
        "fixed_step_2pct_equal_vs_two_early_medium",
        "fixed_step_1pct_equal_vs_legacy",
    ):
        if prefer in pair_sets and pair_sets[prefer]:
            primary_name = prefer
            break
    if primary_name is None:
        primary_name = next(iter(pair_sets))
    primary_pairs = pair_sets[primary_name]

    safety = {
        "economic_undercoverage_closed": sum(
            int(safe_float(r.get("economic_undercoverage_closed"))) for r in raw_rows
        ),
        "invalid_partial": sum(int(safe_float(r.get("invalid_partial"))) for r in raw_rows),
        "over_close": sum(int(safe_float(r.get("over_close"))) for r in raw_rows),
        "duplicate_stage": sum(int(safe_float(r.get("duplicate_stage"))) for r in raw_rows),
        "late_stage_fill_after_exit": sum(
            int(safe_float(r.get("late_stage_fill_after_exit"))) for r in raw_rows
        ),
        "orphan_stage_order": sum(int(safe_float(r.get("orphan_stage_order"))) for r in raw_rows),
        "sufficient_false_closed": sum(
            int(safe_float(r.get("sufficient_false_closed"))) for r in raw_rows
        ),
    }
    missing = [
        pk
        for pk in (str(r["pair_key"]) for r in start_rows)
        if not set(profiles).issubset(set(by_key.get(pk, {})))
    ]
    integrity = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "planned_pairs": len(start_rows),
        "completed_pairs": len(start_rows) - len(missing),
        "missing_pairs": missing[:50],
        "n_missing": len(missing),
        "duplicate_pair_keys": 0,
        "safety": safety,
        "safety_ok": all(v == 0 for v in safety.values()),
        "pass": len(missing) == 0 and all(v == 0 for v in safety.values()),
    }

    summaries = {name: summarize_pairs(pairs) for name, pairs in pair_sets.items()}
    ranking = []
    for name, s in summaries.items():
        if not name.endswith("_vs_legacy"):
            continue
        ranking.append(
            {
                "comparison": name,
                "sum_delta_total_pnl": s["delta_total"]["sum"],
                "sum_delta_closed_pnl": s["sum_delta_closed_pnl"],
                "sum_delta_open_mtm": s["sum_delta_open_mtm"],
                "better": s["better"],
                "equal": s["equal"],
                "worse": s["worse"],
                "additional_valid_closes": s["additional_valid_closes"],
                "lost_valid_closes": s["lost_valid_closes"],
            }
        )
    ranking_sorted = sorted(ranking, key=lambda r: safe_float(r["sum_delta_total_pnl"]), reverse=True)

    write_csv(output_dir / "pair_results.csv", primary_pairs)
    for name, pairs in pair_sets.items():
        write_csv(output_dir / f"pair_results_{name}.csv", pairs)

    write_csv(output_dir / "summary_by_coin.csv", summarize_by_keys(primary_pairs, ["coin"]))
    write_csv(output_dir / "summary_by_distance_bucket.csv", summarize_by_distance_bucket(primary_pairs))
    write_csv(
        output_dir / "summary_by_profile_distance_bucket.csv",
        summarize_by_profile_distance_bucket(raw_rows),
    )
    write_csv(output_dir / "summary_by_grid_step.csv", _summarize_by_grid_step(raw_rows))
    write_csv(
        output_dir / "summary_by_requested_stage_count.csv",
        _summarize_by_stage_count(raw_rows, field="requested_stage_count"),
    )
    write_csv(
        output_dir / "summary_by_effective_stage_count.csv",
        _summarize_by_stage_count(raw_rows, field="effective_stage_count_after_rounding"),
    )

    # comparison_by_profile_bucket: each fixed-step vs TEM by bucket
    comparison_rows = []
    for name, pairs in pair_sets.items():
        if "_vs_two_early_medium" not in name and "_vs_adaptive_equal" not in name:
            continue
        for row in summarize_by_distance_bucket(pairs):
            comparison_rows.append({"comparison": name, **row})
    write_csv(output_dir / "comparison_by_profile_bucket.csv", comparison_rows)

    write_csv(output_dir / "stage_fill_analysis.csv", _stage_fill_analysis(raw_rows))
    write_csv(output_dir / "stage_fee_analysis.csv", _fee_analysis(raw_rows))
    write_csv(output_dir / "min_notional_fallbacks.csv", _min_notional_fallbacks(raw_rows))
    write_csv(output_dir / "blocker_summary_by_profile.csv", blocker_summary_by_profile(raw_rows))
    write_csv(output_dir / "lost_closes.csv", _lost_additional_rows(primary_pairs, kind="lost"))
    write_csv(output_dir / "additional_closes.csv", _lost_additional_rows(primary_pairs, kind="additional"))
    write_csv(output_dir / "worst_cases.csv", _worst_cases(primary_pairs))
    write_csv(output_dir / "coin_universe.csv", included)
    write_csv(output_dir / "excluded_coins.csv", excluded)
    write_csv(output_dir / "time_windows.csv", window_rows)

    # Exposure/drawdown from primary pairs
    from research.backtests.adaptive_distance_staging_metrics import exposure_drawdown_by_bucket

    write_csv(output_dir / "exposure_drawdown.csv", exposure_drawdown_by_bucket(primary_pairs))

    # summary_by_profile
    by_prof: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in raw_rows:
        by_prof[str(r.get("profile") or "")].append(r)
    summary_prof = []
    for prof, subset in sorted(by_prof.items()):
        summary_prof.append(
            {
                "profile": prof,
                "n_runs": len(subset),
                "staging_activated": sum(int(safe_float(r.get("staging_activated"))) for r in subset),
                "sum_total_pnl": sum(safe_float(r.get("total_pnl")) for r in subset),
                "sum_closed_pnl": sum(safe_float(r.get("closed_pnl")) for r in subset),
                "sum_open_mtm": sum(safe_float(r.get("open_mtm")) for r in subset),
                "sum_fees": sum(safe_float(r.get("fees") or r.get("total_fees")) for r in subset),
                "mean_requested_stages": (
                    sum(safe_float(r.get("requested_stage_count")) for r in subset) / len(subset)
                ),
                "mean_effective_stages": (
                    sum(safe_float(r.get("effective_stage_count_after_rounding")) for r in subset)
                    / len(subset)
                ),
                "n_reduce_stage_count": sum(
                    1 for r in subset if str(r.get("fallback_used") or "") == "reduce_stage_count"
                ),
            }
        )
    write_csv(output_dir / "summary_by_profile.csv", summary_prof)

    agg_rows = [{"metric": "n_pairs", "value": len(primary_pairs)}]
    for r in ranking_sorted:
        agg_rows.append(
            {"metric": f"{r['comparison']}_sum_delta_total", "value": r["sum_delta_total_pnl"]}
        )
    agg_rows.append({"metric": "safety_ok", "value": integrity["safety_ok"]})
    write_csv(output_dir / "aggregate_summary.csv", agg_rows)

    # Decision gates
    tem_name = "two_early_medium_vs_legacy"
    tem_sum = summaries.get(tem_name)
    fixed_vs_tem = {
        k: v
        for k, v in summaries.items()
        if k.startswith("fixed_step_") and k.endswith("_vs_two_early_medium")
    }
    recommended = ["legacy", "two_early_medium", "adaptive_equal"]
    best_fixed = None
    best_delta = None
    for name, s in fixed_vs_tem.items():
        dt = s["delta_total"]["sum"] or 0.0
        if best_delta is None or dt > best_delta:
            best_delta = dt
            best_fixed = name.replace("_vs_two_early_medium", "")
    # Min-notional collapse check for 1pct
    fs1 = [r for r in raw_rows if r.get("profile") == "fixed_step_1pct_equal"]
    collapse_rate = 0.0
    if fs1:
        skip_ratio = []
        for r in fs1:
            req = safe_float(r.get("requested_stage_count"))
            eff = safe_float(r.get("effective_stage_count_after_rounding"))
            if req > 0:
                skip_ratio.append(max(req - eff, 0) / req)
        collapse_rate = (sum(skip_ratio) / len(skip_ratio)) if skip_ratio else 0.0
    if best_fixed and (best_delta or 0) > 0 and collapse_rate < 0.5:
        recommended.append(best_fixed)

    decision = {
        "verdict": "preliminary_research_only",
        "note": "Absolute profitability not answered. Relative fixed-step research only.",
        "ranking_vs_legacy": ranking_sorted,
        "integrity_pass": integrity["pass"],
        "safety": safety,
        "comparisons": {k: summaries[k] for k in list(summaries)[:20]},
        "fixed_step_vs_tem": {
            k: {
                "sum_delta_total": summaries[k]["delta_total"]["sum"],
                "better": summaries[k]["better"],
                "worse": summaries[k]["worse"],
                "add": summaries[k]["additional_valid_closes"],
                "lost": summaries[k]["lost_valid_closes"],
            }
            for k in fixed_vs_tem
        },
        "one_pct_mean_stage_collapse_rate": collapse_rate,
        "recommended_full_run_profiles": recommended,
        "best_fixed_vs_tem": best_fixed,
        "full_run_justified": bool(
            integrity["pass"]
            and best_fixed
            and (best_delta or 0) > 0
            and collapse_rate < 0.5
        ),
        "live_integration": "keine Live-Integration",
    }

    atomic_write_json(output_dir / "integrity.json", integrity)
    atomic_write_json(output_dir / "decision_preliminary.json", decision)
    atomic_write_json(output_dir / "run_manifest.json", manifest)
    atomic_write_json(output_dir / "comparison_summaries.json", summaries)

    report = [
        "# Fixed-Step Distance Staging Validation (Research)",
        "",
        f"Generated: `{integrity['generated_at']}`",
        f"Mode: `{manifest.get('mode')}`",
        f"Profiles: `{list(profiles)}`",
        f"Pairs: **{len(start_rows)}**",
        "",
        "## Safety",
        "",
        f"- pass: **{integrity['pass']}**",
        f"- safety: `{json.dumps(safety)}`",
        "",
        "## Ranking vs legacy (Δ total)",
        "",
    ]
    for r in ranking_sorted:
        report.append(
            f"- `{r['comparison']}`: Δtotal={r['sum_delta_total_pnl']}, "
            f"B/E/W={r['better']}/{r['equal']}/{r['worse']}"
        )
    report.extend(
        [
            "",
            "## Fixed-step vs TEM",
            "",
            json.dumps(decision["fixed_step_vs_tem"], indent=2, default=str),
            "",
            f"## 1%-mean stage collapse rate: **{collapse_rate:.3f}**",
            f"## Recommended full-run profiles: `{recommended}`",
            f"## full_run_justified: **{decision['full_run_justified']}**",
            "",
            "Live: keine Live-Integration",
            "",
        ]
    )
    (output_dir / "REPORT.md").write_text("\n".join(report) + "\n", encoding="utf-8")
    return {"integrity": integrity, "decision": decision, "summaries": summaries, "n_pairs": len(primary_pairs)}


DIAG_COLS = (
    "original_distance_pct",
    "distance_bucket",
    "theoretical_distance_bucket",
    "distance_status",
    "grid_step_pct",
    "requested_absolute_stage_distances_pct",
    "effective_absolute_stage_distances_pct",
    "requested_price_fractions",
    "effective_price_fractions",
    "requested_stage_count",
    "capped_stage_count",
    "stage_cap_applied",
    "selected_stage_count",
    "selected_price_fractions",
    "selected_qty_fractions",
    "requested_qty_fractions",
    "effective_qty_fractions",
    "effective_stage_count_after_rounding",
    "skipped_small_stages",
    "merged_stage_count",
    "residual_qty",
    "stage_activation_count",
    "stage_fill_count",
    "first_stage_fill_delay",
    "last_stage_fill_delay",
    "unfilled_stage_indices",
    "max_gross_exposure",
    "fees",
)


def run_validation(
    *,
    output_dir: Path,
    candle_limit: int | None,
    target_per_window: int,
    seed: int,
    warmup: int,
    smoke: bool,
    resume: bool,
    smoke_coins: list[str] | None,
    coins: list[str] | None,
    max_windows_per_coin: int | None,
    max_pairs: int | None,
    mode: str,
    profiles: Sequence[str],
    starts_csv: Path | None = None,
) -> dict[str, Any]:
    assert_output_dir_safe(output_dir, resume=resume)
    for protected in PROTECTED:
        if output_dir.resolve() == protected.resolve():
            raise RuntimeError(f"refusing protected output dir: {protected}")
    output_dir.mkdir(parents=True, exist_ok=True)
    profiles = tuple(profiles)

    paths = {
        "start": output_dir / "start_points.csv",
        "raw": output_dir / "raw_profile_runs.csv",
        "ck": output_dir / "checkpoint.json",
        "manifest": output_dir / "run_manifest.json",
        "universe": output_dir / "coin_universe.csv",
        "excluded": output_dir / "excluded_coins.csv",
        "windows": output_dir / "time_windows.csv",
    }

    if resume and paths["manifest"].exists() and paths["start"].exists():
        manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))
        start_rows = load_csv_rows(paths["start"])
        included = load_csv_rows(paths["universe"])
        excluded = load_csv_rows(paths["excluded"])
        window_rows = load_csv_rows(paths["windows"])
        profiles = tuple(manifest.get("profiles") or profiles)
        coin_candles: dict[str, list[Any]] = {}
        for coin in manifest["coins"]:
            coin_candles[coin] = normalize_candles(
                coin, load_candles_for_symbol(coin, limit=manifest.get("candle_limit"))
            )
        log(f"[resume] {len(start_rows)} planned pairs")
    elif mode in {"bucket_coverage", "synthetic"} or starts_csv is not None:
        if starts_csv is None:
            raise RuntimeError(f"{mode} requires --starts <selected_starts.csv>")
        if not starts_csv.exists():
            raise FileNotFoundError(f"starts csv not found: {starts_csv}")
        start_rows = load_csv_rows(starts_csv)
        if max_pairs is not None:
            start_rows = start_rows[: int(max_pairs)]
        coins_needed = sorted({str(r["coin"]).upper() for r in start_rows})
        coin_candles = {}
        included = []
        for coin in coins_needed:
            coin_candles[coin] = normalize_candles(
                coin, load_candles_for_symbol(coin, limit=candle_limit)
            )
            included.append(
                {
                    "coin": coin,
                    "n_candles": len(coin_candles[coin]),
                    "source": f"{mode}_starts",
                    "included": True,
                }
            )
        excluded = []
        window_rows = []
        seen_w: set[tuple[str, str]] = set()
        for r in start_rows:
            key = (str(r["coin"]).upper(), str(r.get("window_id") or ""))
            if key in seen_w:
                continue
            seen_w.add(key)
            window_rows.append(
                {
                    "coin": key[0],
                    "window_id": key[1],
                    "window_kind": r.get("window_kind") or key[1],
                    "source": f"{mode}_starts",
                }
            )
        manifest = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "mode": mode,
            "starts_csv": str(starts_csv),
            "profiles": list(profiles),
            "coins": coins_needed,
            "n_coins": len(coins_needed),
            "n_planned_pairs": len(start_rows),
            "n_planned_profile_runs": len(start_rows) * len(profiles),
            "candle_limit": candle_limit,
            "selection": "fixed_starts_no_reselection",
        }
        write_csv(paths["universe"], included)
        write_csv(paths["excluded"], excluded)
        write_csv(paths["windows"], window_rows)
        write_csv(paths["start"], start_rows)
        atomic_write_json(paths["manifest"], manifest)
        atomic_write_json(
            paths["ck"],
            _empty_checkpoint(coins=manifest["coins"], planned_pairs=len(start_rows), profiles=profiles),
        )
        log(f"[{mode}] loaded {len(start_rows)} fixed starts from {starts_csv}")
    else:
        included, excluded, window_rows, start_rows, coin_candles, manifest = plan_universe_and_starts(
            candle_limit=candle_limit,
            target_per_window=target_per_window,
            seed=seed,
            warmup=warmup,
            smoke=bool(smoke),
            smoke_coins=smoke_coins if smoke else None,
            max_windows_per_coin=max_windows_per_coin,
        )
        if coins:
            want = {c.upper() for c in coins}
            for c in sorted(want):
                if c not in coin_candles:
                    raw = load_candles_for_symbol(c, limit=candle_limit)
                    coin_candles[c] = normalize_candles(c, raw)
                    included.append(
                        {
                            "coin": c,
                            "n_candles": len(coin_candles[c]),
                            "source": "forced_coin_list",
                            "included": True,
                        }
                    )
            start_rows = [r for r in start_rows if str(r["coin"]).upper() in want]
            included = [r for r in included if str(r.get("coin") or "").upper() in want]
            window_rows = [r for r in window_rows if str(r.get("coin") or "").upper() in want]
            coin_candles = {k: v for k, v in coin_candles.items() if k in want}
        if max_pairs is not None:
            start_rows = start_rows[: int(max_pairs)]
        manifest["profiles"] = list(profiles)
        manifest["coins"] = sorted(coin_candles.keys())
        manifest["n_coins"] = len(manifest["coins"])
        manifest["n_planned_pairs"] = len(start_rows)
        manifest["n_planned_profile_runs"] = len(start_rows) * len(profiles)
        manifest["mode"] = mode
        write_csv(paths["universe"], included)
        write_csv(paths["excluded"], excluded)
        write_csv(paths["windows"], window_rows)
        write_csv(paths["start"], start_rows)
        atomic_write_json(paths["manifest"], manifest)
        atomic_write_json(
            paths["ck"],
            _empty_checkpoint(coins=manifest["coins"], planned_pairs=len(start_rows), profiles=profiles),
        )

    ck = load_checkpoint(paths["ck"]) or _empty_checkpoint(
        coins=list(manifest["coins"]), planned_pairs=len(start_rows), profiles=profiles
    )
    raw_rows = load_csv_rows(paths["raw"])
    profiles_by_pair: dict[str, set[str]] = defaultdict(set)
    done_runs: set[str] = set()
    for r in raw_rows:
        done_runs.add(str(r.get("run_key")))
        profiles_by_pair[str(r.get("pair_key"))].add(str(r.get("profile")))
    done_pairs = {pk for pk, ps in profiles_by_pair.items() if set(profiles).issubset(ps)}

    raw_fields: list[str] | None = list(raw_rows[0].keys()) if raw_rows else None
    t0 = time.time()
    planned = len(start_rows)

    for i, sp in enumerate(start_rows, start=1):
        pk = str(sp["pair_key"])
        coin = str(sp["coin"])
        start_index = int(sp["start_index"])
        window_id = str(sp["window_id"])
        max_win = int(sp["max_window_candles"])
        if pk in done_pairs:
            continue
        log(f"[{i}/{planned}] {pk}")
        candles = coin_candles[coin]
        for profile in profiles:
            rk = window_profile_run_key(coin, window_id, start_index, profile)
            if rk in done_runs:
                continue
            try:
                row = run_profile_at_start(
                    coin=coin,
                    start_index=start_index,
                    profile=profile,
                    candles=candles,
                    max_window_candles=max_win,
                    capture_economics=(profile != "legacy"),
                )
                row["pair_key"] = pk
                row["run_key"] = rk
                row["window_id"] = window_id
                row["window_kind"] = sp.get("window_kind")
                row["run_end_index"] = sp.get("run_end_index")
                row["max_window_candles"] = max_win
                row["primary_category"] = sp.get("primary_category")
                row["categories"] = sp.get("categories")
                row["is_historical_blocker"] = sp.get("is_historical_blocker")
                row["is_neutral_pool"] = sp.get("is_neutral_pool")
                if raw_fields is None:
                    raw_fields = list(row.keys())
                    for col in DIAG_COLS:
                        if col not in raw_fields:
                            raw_fields.append(col)
                _append_csv_row(paths["raw"], row, raw_fields)
                raw_rows.append(row)
                done_runs.add(rk)
            except Exception as exc:  # noqa: BLE001
                err = {"run_key": rk, "error": str(exc), "traceback": traceback.format_exc()}
                ck.setdefault("errors", []).append(err)
                atomic_write_json(paths["ck"], ck)
                log(f"  ERROR {rk}: {exc}")
                raise
        done_pairs.add(pk)
        ck["completed_pair_keys"] = sorted(done_pairs)
        ck["completed_run_keys"] = sorted(done_runs)
        ck["updated_at"] = datetime.now(timezone.utc).isoformat()
        atomic_write_json(paths["ck"], ck)

    manifest["elapsed_sec"] = round(time.time() - t0, 2)
    manifest["completed_pairs"] = len(done_pairs)
    atomic_write_json(paths["manifest"], manifest)

    result = build_artifacts(
        output_dir=output_dir,
        included=included if isinstance(included, list) else load_csv_rows(paths["universe"]),
        excluded=excluded if isinstance(excluded, list) else load_csv_rows(paths["excluded"]),
        window_rows=window_rows,
        start_rows=start_rows,
        raw_rows=raw_rows,
        manifest=manifest,
        profiles=profiles,
    )
    log(
        json.dumps(
            {
                "integrity_pass": result["integrity"]["pass"],
                "n_pairs": result["n_pairs"],
                "full_run_justified": result["decision"].get("full_run_justified"),
                "recommended": result["decision"].get("recommended_full_run_profiles"),
                "elapsed_sec": manifest["elapsed_sec"],
                "output_dir": str(output_dir),
            },
            indent=2,
        )
    )
    return result


def print_manual_commands(
    *,
    output_dir: Path,
    log_dir: Path = DEFAULT_LOG_DIR,
    profiles: Sequence[str] = ("legacy", "two_early_medium", "adaptive_equal", "fixed_step_2pct_equal"),
) -> None:
    out = str(output_dir)
    logs = str(log_dir)
    prof = ",".join(profiles)
    print(
        "\n=== MANUAL FULL RUN (do not auto-start — start later by hand) ===\n"
        f"cd {ROOT} || exit 1\n"
        f"mkdir -p {logs}\n\n"
        f'OUT="{out}"\n'
        f'LOG="{logs}/fixed_step_distance_staging_large.out"\n'
        f'PID_FILE="{logs}/fixed_step_distance_staging_large.pid"\n\n'
        f"nohup env PYTHONPATH=. python -m research.backtests.run_fixed_step_distance_staging_validation \\\n"
        f"  --mode full \\\n"
        f"  --output-dir \"$OUT\" \\\n"
        f"  --target-per-window 25 \\\n"
        f"  --seed 20260722 \\\n"
        f"  --profiles {prof} \\\n"
        f"  > \"$LOG\" 2>&1 &\n\n"
        f'echo $! > \"$PID_FILE\"\n'
        f'echo \"PID: $(cat \"$PID_FILE\")\"\n\n'
        f"# Resume:\n"
        f"nohup env PYTHONPATH=. python -m research.backtests.run_fixed_step_distance_staging_validation \\\n"
        f"  --mode full --resume --output-dir \"$OUT\" --profiles {prof} \\\n"
        f"  >> \"$LOG\" 2>&1 &\n"
        f'echo $! > \"$PID_FILE\"\n\n'
        f"# Follow / status:\n"
        f'tail -f \"$LOG\"\n'
        f'ps -p $(cat \"$PID_FILE\") -o pid,etime,cmd\n'
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--log-dir", type=Path, default=DEFAULT_LOG_DIR)
    parser.add_argument("--candle-limit", type=int, default=FULL_HISTORY_CANDLE_LIMIT)
    parser.add_argument("--target-per-window", type=int, default=TARGET_STARTS_PER_WINDOW)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--warmup", type=int, default=DEFAULT_WARMUP)
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument("--smoke-coins", default="APTUSDT,BTCUSDT,ETHUSDT")
    parser.add_argument("--coins", default=None)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--max-windows-per-coin", type=int, default=None)
    parser.add_argument("--max-pairs", type=int, default=None)
    parser.add_argument(
        "--mode",
        default="manual",
        choices=("synthetic", "bucket_coverage", "stage_c", "full", "manual"),
    )
    parser.add_argument("--starts", type=Path, default=None)
    parser.add_argument(
        "--profiles",
        default=",".join(DEFAULT_PROFILES),
        help="Comma-separated profile list",
    )
    parser.add_argument("--print-manual-commands-only", action="store_true")
    args = parser.parse_args(argv)

    profiles = tuple(p.strip() for p in str(args.profiles).split(",") if p.strip())
    if args.print_manual_commands_only:
        print_manual_commands(output_dir=args.output_dir, log_dir=args.log_dir, profiles=profiles)
        return 0

    if args.mode == "manual" and not args.smoke and args.coins is None and args.max_pairs is None and args.starts is None:
        log("mode=manual without limits — printing recipe only.")
        print_manual_commands(output_dir=args.output_dir, log_dir=args.log_dir, profiles=profiles)
        return 0

    smoke_coins = [c.strip().upper() for c in str(args.smoke_coins).split(",") if c.strip()]
    coin_list = None
    if args.coins:
        coin_list = [c.strip().upper() for c in str(args.coins).split(",") if c.strip()]
    elif args.mode == "stage_c":
        coin_list = list(STAGE_C_COINS)

    mode = args.mode
    starts = args.starts
    max_windows = args.max_windows_per_coin
    target = args.target_per_window
    max_pairs = args.max_pairs

    if mode == "bucket_coverage":
        if starts is None:
            starts = DEFAULT_BUCKET_STARTS
    elif mode == "stage_c":
        max_windows = 4 if max_windows is None else max_windows
        target = min(8, target) if max_pairs is None else target
        if max_pairs is None:
            max_pairs = 320
    elif mode == "full":
        log("WARNING: mode=full — manual 27-coin campaign only.")
    elif mode == "synthetic":
        if starts is None:
            raise SystemExit("synthetic mode expects --starts pointing at a tiny fixture CSV")

    run_validation(
        output_dir=args.output_dir,
        candle_limit=args.candle_limit,
        target_per_window=target,
        seed=args.seed,
        warmup=args.warmup,
        smoke=bool(args.smoke),
        resume=bool(args.resume),
        smoke_coins=smoke_coins if args.smoke else None,
        coins=coin_list,
        max_windows_per_coin=max_windows,
        max_pairs=max_pairs,
        mode=mode,
        profiles=profiles,
        starts_csv=starts,
    )
    if mode != "full":
        print_manual_commands(
            output_dir=ROOT
            / "research/backtests/results/fixed_step_distance_staging_large_1000_500_YYYYMMDD",
            log_dir=args.log_dir,
            profiles=profiles[:4] if len(profiles) > 4 else profiles,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

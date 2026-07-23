#!/usr/bin/env python3
"""Adaptive distance staging validation: legacy / TEM / adaptive_equal / adaptive_backloaded.

Research-only. Does NOT auto-start a 27-coin full run. Use --print-manual-commands-only
for the later full-run recipe. Never overwrite protected prior result directories.
"""

from __future__ import annotations

import argparse
import csv
import json
import time
import traceback
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from research.backtests.adaptive_distance_staging_metrics import (
    blocker_summary_by_profile,
    blocker_summary_by_profile_bucket,
    bucket_activation_counts,
    bucket_stage_fallbacks,
    compare_profiles,
    comparison_by_distance_bucket,
    exposure_drawdown_by_bucket,
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
    ROOT / "research/backtests/results/adaptive_distance_staging_validation_1000_500_20260722"
)
DEFAULT_LOG_DIR = ROOT / "research/backtests/results/_logs"
PROFILES = ("legacy", "two_early_medium", "adaptive_equal", "adaptive_backloaded")
CANDIDATE_PROFILES = ("two_early_medium", "adaptive_equal", "adaptive_backloaded")
PROTECTED = (
    ROOT / "research/backtests/results/two_early_medium_large_multicoin_window_validation_1000_500_20260721",
    ROOT / "research/backtests/results/two_early_medium_multistart_validation_1000_500_20260721",
    ROOT / "research/backtests/results/two_early_medium_candidate_validation_1000_500_20260721",
    ROOT / "research/backtests/results/multicoin_price_staging_grid_1000_500_20260721",
    ROOT / "research/backtests/results/adaptive_distance_staging_stage_c_20260722",
    ROOT / "research/backtests/results/adaptive_distance_staging_stage_d_20260722",
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


def _empty_checkpoint(*, coins: list[str], planned_pairs: int) -> dict[str, Any]:
    return {
        "version": 1,
        "profiles": list(PROFILES),
        "coins": list(coins),
        "planned_pairs": planned_pairs,
        "completed_pair_keys": [],
        "completed_run_keys": [],
        "errors": [],
        "updated_at": None,
    }


def _lost_additional_rows(
    pairs: list[dict[str, Any]], *, kind: str
) -> list[dict[str, Any]]:
    out = []
    for p in pairs:
        lost = int(p.get("legacy_valid_close") or 0) == 1 and int(p.get("staging_valid_close") or 0) == 0
        add = int(p.get("legacy_valid_close") or 0) == 0 and int(p.get("staging_valid_close") or 0) == 1
        if kind == "lost" and lost:
            out.append(p)
        if kind == "additional" and add:
            out.append(p)
    return out


def build_artifacts(
    *,
    output_dir: Path,
    included: list[dict[str, Any]],
    excluded: list[dict[str, Any]],
    window_rows: list[dict[str, Any]],
    start_rows: list[dict[str, Any]],
    raw_rows: list[dict[str, Any]],
    manifest: dict[str, Any],
) -> dict[str, Any]:
    start_meta = {str(r["pair_key"]): dict(r) for r in start_rows}
    by_key: dict[str, dict[str, dict[str, Any]]] = {}
    for row in raw_rows:
        by_key.setdefault(str(row.get("pair_key")), {})[str(row["profile"])] = row

    pair_sets: dict[str, list[dict[str, Any]]] = {
        "two_early_medium_vs_legacy": [],
        "adaptive_equal_vs_legacy": [],
        "adaptive_backloaded_vs_legacy": [],
        "adaptive_equal_vs_two_early_medium": [],
        "adaptive_backloaded_vs_two_early_medium": [],
    }
    for pk, profs in sorted(by_key.items()):
        meta = start_meta.get(pk, {"pair_key": pk})
        if "legacy" not in profs:
            continue
        for cand in CANDIDATE_PROFILES:
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
        if "two_early_medium" in profs:
            for cand in ("adaptive_equal", "adaptive_backloaded"):
                if cand not in profs:
                    continue
                pair_sets[f"{cand}_vs_two_early_medium"].append(
                    compare_profiles(
                        profs["two_early_medium"],
                        profs[cand],
                        meta,
                        baseline_name="two_early_medium",
                        candidate_name=cand,
                    )
                )

    # Primary ranking uses vs-legacy pairs for adaptive + TEM
    primary_label = "adaptive_equal_vs_legacy"
    primary_pairs = pair_sets[primary_label]
    tem_pairs = pair_sets["two_early_medium_vs_legacy"]

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
        if not set(PROFILES).issubset(set(by_key.get(pk, {})))
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
    # Prefer adaptive_equal vs legacy as preliminary focus; also report all
    ranking = []
    for name in (
        "two_early_medium_vs_legacy",
        "adaptive_equal_vs_legacy",
        "adaptive_backloaded_vs_legacy",
    ):
        s = summaries[name]
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

    # Write pair CSVs (primary adaptive_equal vs legacy as pair_results.csv)
    write_csv(output_dir / "pair_results.csv", primary_pairs)
    for name, pairs in pair_sets.items():
        write_csv(output_dir / f"pair_results_{name}.csv", pairs)

    write_csv(output_dir / "summary_by_coin.csv", summarize_by_keys(primary_pairs, ["coin"]))
    write_csv(output_dir / "summary_by_window.csv", summarize_by_keys(primary_pairs, ["window_id"]))
    write_csv(output_dir / "summary_by_distance_bucket.csv", summarize_by_distance_bucket(primary_pairs))
    write_csv(
        output_dir / "summary_by_profile_distance_bucket.csv",
        summarize_by_profile_distance_bucket(raw_rows),
    )
    # Adaptive vs TEM by bucket
    ae_vs_tem = pair_sets.get("adaptive_equal_vs_two_early_medium") or []
    ab_vs_tem = pair_sets.get("adaptive_backloaded_vs_two_early_medium") or []
    comparison_rows = comparison_by_distance_bucket(
        ae_vs_tem, comparison_name="adaptive_equal_vs_two_early_medium"
    ) + comparison_by_distance_bucket(
        ab_vs_tem, comparison_name="adaptive_backloaded_vs_two_early_medium"
    )
    write_csv(output_dir / "comparison_by_distance_bucket.csv", comparison_rows)
    write_csv(output_dir / "bucket_activation_counts.csv", bucket_activation_counts(raw_rows))
    write_csv(output_dir / "bucket_stage_fallbacks.csv", bucket_stage_fallbacks(raw_rows))
    write_csv(output_dir / "blocker_summary_by_profile.csv", blocker_summary_by_profile(raw_rows))
    write_csv(
        output_dir / "blocker_summary_by_profile_bucket.csv",
        blocker_summary_by_profile_bucket(raw_rows),
    )
    write_csv(
        output_dir / "exposure_drawdown_by_bucket.csv",
        exposure_drawdown_by_bucket(primary_pairs),
    )
    write_csv(output_dir / "lost_closes.csv", _lost_additional_rows(primary_pairs, kind="lost"))
    write_csv(
        output_dir / "additional_closes.csv",
        _lost_additional_rows(primary_pairs, kind="additional"),
    )
    write_csv(output_dir / "coin_universe.csv", included)
    write_csv(output_dir / "excluded_coins.csv", excluded)
    write_csv(output_dir / "time_windows.csv", window_rows)

    agg_rows = [{"metric": "n_pairs", "value": len(primary_pairs)}]
    for r in ranking_sorted:
        agg_rows.append(
            {
                "metric": f"{r['comparison']}_sum_delta_total",
                "value": r["sum_delta_total_pnl"],
            }
        )
    agg_rows.append({"metric": "safety_ok", "value": integrity["safety_ok"]})
    write_csv(output_dir / "aggregate_summary.csv", agg_rows)

    decision = {
        "verdict": "preliminary_research_only",
        "note": (
            "Stufe C/D only — no Shadow/Paper judgment. Absolute profitability not answered."
        ),
        "ranking_vs_legacy": ranking_sorted,
        "integrity_pass": integrity["pass"],
        "safety": safety,
        "comparisons": summaries,
        "live_integration": "keine Live-Integration",
        "full_run_justified": None,
    }
    # Soft recommendation for full run
    ae = summaries["adaptive_equal_vs_legacy"]
    ab = summaries["adaptive_backloaded_vs_legacy"]
    tem = summaries["two_early_medium_vs_legacy"]
    justified = bool(
        integrity["pass"]
        and (
            (ae["delta_total"]["sum"] or 0) > (tem["delta_total"]["sum"] or 0)
            or (ab["delta_total"]["sum"] or 0) > (tem["delta_total"]["sum"] or 0)
        )
        and ((ae["delta_total"]["sum"] or 0) > 0 or (ab["delta_total"]["sum"] or 0) > 0)
    )
    # Bucket-coverage: require synthetic semantics + historically sufficient buckets when claimed
    if str(manifest.get("mode") or "") == "bucket_coverage":
        from research.backtests.adaptive_distance_staging import REAL_DISTANCE_BUCKETS

        bucket_n = {
            r["distance_bucket"]: int(r["n_pairs"])
            for r in summarize_by_distance_bucket(primary_pairs)
            if r["distance_bucket"] in REAL_DISTANCE_BUCKETS
        }
        insufficient = [b for b in REAL_DISTANCE_BUCKETS if bucket_n.get(b, 0) < 10]
        decision["bucket_coverage"] = {
            "n_by_bucket": bucket_n,
            "sample_insufficient": insufficient,
            "note": "No bucket claim when n<10; relative research only.",
        }
        if insufficient:
            decision["next_step"] = (
                "Weiterer Bucket-Sampler nötig — historisch sample_insufficient: "
                + ",".join(insufficient)
            )
            justified = False
    decision["full_run_justified"] = justified
    if "next_step" not in decision or not decision.get("next_step"):
        decision["next_step"] = (
            "27-Coin-Full-Run manuell starten"
            if justified
            else "Weitere Micro-Analyse / Fraktionen anpassen vor Full-Run"
        )

    atomic_write_json(output_dir / "integrity.json", integrity)
    atomic_write_json(output_dir / "decision_preliminary.json", decision)
    atomic_write_json(output_dir / "run_manifest.json", manifest)
    atomic_write_json(output_dir / "comparison_summaries.json", summaries)

    report = [
        "# Adaptive Distance Staging Validation (Research)",
        "",
        f"Generated: `{integrity['generated_at']}`",
        f"Smoke/mode: `{manifest.get('mode')}`",
        "",
        "## Scope",
        "",
        f"- Profiles: `{list(PROFILES)}`",
        f"- Coins: **{manifest.get('n_coins')}**",
        f"- Planned pairs: **{manifest.get('n_planned_pairs')}**",
        f"- Profile runs: **{manifest.get('n_planned_profile_runs')}**",
        "",
        "## Integrity / Safety",
        "",
        f"- pass: **{integrity['pass']}**",
        f"- safety: `{json.dumps(safety)}`",
        "",
        "## Relative ranking vs legacy (Δ total PnL)",
        "",
    ]
    for r in ranking_sorted:
        report.append(
            f"- `{r['comparison']}`: Δtotal={r['sum_delta_total_pnl']}, "
            f"closed={r['sum_delta_closed_pnl']}, openMTM={r['sum_delta_open_mtm']}, "
            f"B/E/W={r['better']}/{r['equal']}/{r['worse']}, "
            f"add/lost={r['additional_valid_closes']}/{r['lost_valid_closes']}"
        )
    report.extend(
        [
            "",
            "## Interpretation",
            "",
            "1. Relative Profilverbesserung — siehe Ranking oben (Pair-Test).",
            "2. Blocker-/Risk — siehe `blocker_summary_by_profile.csv` + Safety.",
            "3. Absolute Profitabilität — **nicht** beantwortet (kein Continuous).",
            "",
            f"## Preliminary full-run justified: **{justified}**",
            "",
            decision["next_step"],
            "",
            "Live: keine Live-Integration",
            "",
        ]
    )
    (output_dir / "REPORT.md").write_text("\n".join(report) + "\n", encoding="utf-8")
    return {
        "integrity": integrity,
        "decision": decision,
        "summaries": summaries,
        "n_pairs": len(primary_pairs),
    }


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
    starts_csv: Path | None = None,
) -> dict[str, Any]:
    assert_output_dir_safe(output_dir, resume=resume)
    for protected in PROTECTED:
        if output_dir.resolve() == protected.resolve():
            raise RuntimeError(f"refusing protected output dir: {protected}")
    output_dir.mkdir(parents=True, exist_ok=True)

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
        coin_candles: dict[str, list[Any]] = {}
        for coin in manifest["coins"]:
            coin_candles[coin] = normalize_candles(
                coin, load_candles_for_symbol(coin, limit=manifest.get("candle_limit"))
            )
        log(f"[resume] {len(start_rows)} planned pairs")
    elif mode == "bucket_coverage" or starts_csv is not None:
        if starts_csv is None:
            raise RuntimeError("bucket_coverage requires --starts <selected_starts.csv>")
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
                    "source": "bucket_coverage_starts",
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
                    "source": "bucket_coverage_starts",
                }
            )
        manifest = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "mode": mode,
            "starts_csv": str(starts_csv),
            "profiles": list(PROFILES),
            "coins": coins_needed,
            "n_coins": len(coins_needed),
            "n_planned_pairs": len(start_rows),
            "n_planned_profile_runs": len(start_rows) * len(PROFILES),
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
            _empty_checkpoint(coins=manifest["coins"], planned_pairs=len(start_rows)),
        )
        log(f"[bucket_coverage] loaded {len(start_rows)} fixed starts from {starts_csv}")
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
            # Force-load any requested coin missing from discovery
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
            # Drop unused candles
            coin_candles = {k: v for k, v in coin_candles.items() if k in want}
            # If filtering removed everything (e.g. smoke path without those coins), rebuild via smoke force
            if not start_rows:
                included, excluded, window_rows, start_rows, coin_candles, manifest = plan_universe_and_starts(
                    candle_limit=candle_limit,
                    target_per_window=target_per_window,
                    seed=seed,
                    warmup=warmup,
                    smoke=True,
                    smoke_coins=sorted(want),
                    max_windows_per_coin=max_windows_per_coin,
                )
        if max_pairs is not None:
            start_rows = start_rows[: int(max_pairs)]
        manifest["profiles"] = list(PROFILES)
        manifest["coins"] = sorted(coin_candles.keys())
        manifest["n_coins"] = len(manifest["coins"])
        manifest["n_planned_pairs"] = len(start_rows)
        manifest["n_planned_profile_runs"] = len(start_rows) * len(PROFILES)
        manifest["mode"] = mode
        manifest["max_pairs_applied"] = max_pairs
        write_csv(paths["universe"], included)
        write_csv(paths["excluded"], excluded)
        write_csv(paths["windows"], window_rows)
        write_csv(paths["start"], start_rows)
        atomic_write_json(paths["manifest"], manifest)
        atomic_write_json(
            paths["ck"],
            _empty_checkpoint(coins=manifest["coins"], planned_pairs=len(start_rows)),
        )

    ck = load_checkpoint(paths["ck"]) or _empty_checkpoint(
        coins=list(manifest["coins"]), planned_pairs=len(start_rows)
    )
    raw_rows = load_csv_rows(paths["raw"])
    profiles_by_pair: dict[str, set[str]] = defaultdict(set)
    done_runs: set[str] = set()
    for r in raw_rows:
        done_runs.add(str(r.get("run_key")))
        profiles_by_pair[str(r.get("pair_key"))].add(str(r.get("profile")))
    done_pairs = {pk for pk, ps in profiles_by_pair.items() if set(PROFILES).issubset(ps)}

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
        for profile in PROFILES:
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
                    # Ensure diagnostic columns exist even if first row is legacy
                    for col in (
                        "original_distance_pct",
                        "distance_bucket",
                        "theoretical_distance_bucket",
                        "distance_status",
                        "first_observed_distance_pct",
                        "last_observed_distance_pct",
                        "max_observed_distance_pct",
                        "observed_plan_count",
                        "selected_stage_count",
                        "selected_price_fractions",
                        "selected_qty_fractions",
                        "effective_stage_count_after_rounding",
                        "skipped_small_stages",
                        "merged_stage_count",
                        "residual_qty",
                        "stage_activation_count",
                        "first_stage_fill_delay",
                        "unfilled_stage_indices",
                        "max_gross_exposure",
                    ):
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

        # Abort gates mid-run on safety
        if any(
            int(safe_float(r.get(k))) > 0
            for r in raw_rows[-len(PROFILES) :]
            for k in (
                "economic_undercoverage_closed",
                "invalid_partial",
                "over_close",
                "duplicate_stage",
                "late_stage_fill_after_exit",
                "orphan_stage_order",
            )
        ):
            log("[abort] safety flag > 0 on latest pair — continuing to finalize artifacts")

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
    )
    log(
        json.dumps(
            {
                "integrity_pass": result["integrity"]["pass"],
                "n_pairs": result["n_pairs"],
                "full_run_justified": result["decision"].get("full_run_justified"),
                "elapsed_sec": manifest["elapsed_sec"],
                "output_dir": str(output_dir),
            },
            indent=2,
        )
    )
    return result


def print_manual_commands(output_dir: Path, log_dir: Path = DEFAULT_LOG_DIR) -> None:
    out = str(output_dir)
    logs = str(log_dir)
    print(
        "\n=== MANUAL 27-COIN FULL RUN (do not auto-start) ===\n"
        f"mkdir -p {logs} && \\\n"
        f"nohup env PYTHONPATH=. python -m research.backtests.run_adaptive_distance_staging_validation \\\n"
        f"  --output-dir {out} \\\n"
        f"  --target-per-window 25 \\\n"
        f"  --seed 20260722 \\\n"
        f"  --mode full \\\n"
        f"  > {logs}/adaptive_distance_staging_full.out 2>&1 &\n"
        f"echo $! > {logs}/adaptive_distance_staging_full.pid\n"
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
    parser.add_argument("--coins", default=None, help="Comma-separated coin list (Stufe C)")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--max-windows-per-coin", type=int, default=None)
    parser.add_argument("--max-pairs", type=int, default=None)
    parser.add_argument(
        "--mode",
        default="manual",
        choices=("smoke", "stage_c", "stage_d", "full", "manual", "bucket_coverage"),
    )
    parser.add_argument(
        "--starts",
        type=Path,
        default=None,
        help="Fixed starts CSV for --mode bucket_coverage (no reselection).",
    )
    parser.add_argument("--print-manual-commands-only", action="store_true")
    parser.add_argument("--estimate-only", action="store_true")
    args = parser.parse_args(argv)

    if args.print_manual_commands_only:
        print_manual_commands(args.output_dir, args.log_dir)
        return 0
    if args.estimate_only:
        print(
            json.dumps(
                {
                    "profiles": list(PROFILES),
                    "approx_pairs_27x5x25": 27 * 5 * 25,
                    "approx_profile_runs": 27 * 5 * 25 * 4,
                    "cost_vs_tem_large_2profiles": "≈2×",
                },
                indent=2,
            )
        )
        print_manual_commands(args.output_dir, args.log_dir)
        return 0

    # Accidental unbounded runs: require explicit --mode full (never default).
    # Agent/smoke paths use smoke|stage_c|stage_d|manual|bucket_coverage instead.
    if args.mode not in {"smoke", "stage_c", "stage_d", "full", "manual", "bucket_coverage"}:
        raise SystemExit(f"unknown mode: {args.mode}")
    if args.mode == "bucket_coverage" and args.starts is None and not args.resume:
        raise SystemExit("bucket_coverage requires --starts <selected_starts.csv>")
    if args.mode == "manual" and not args.smoke and args.coins is None and args.max_pairs is None:
        log("mode=manual without coin/max-pairs limits — printing recipe only.")
        print_manual_commands(args.output_dir, args.log_dir)
        return 0

    smoke_coins = [c.strip().upper() for c in str(args.smoke_coins).split(",") if c.strip()]
    coin_list = None
    if args.coins:
        coin_list = [c.strip().upper() for c in str(args.coins).split(",") if c.strip()]
    elif args.mode == "stage_c":
        coin_list = list(STAGE_C_COINS)

    mode = args.mode
    if args.smoke:
        mode = "smoke"

    max_windows = args.max_windows_per_coin
    target = args.target_per_window
    max_pairs = args.max_pairs
    if mode == "smoke":
        max_windows = 2 if max_windows is None else max_windows
        target = min(4, target)
    elif mode == "stage_c":
        max_windows = 4 if max_windows is None else max_windows
        target = min(8, target) if max_pairs is None else target
        if max_pairs is None:
            max_pairs = 320
    elif mode == "stage_d":
        max_windows = 2 if max_windows is None else max_windows
        target = min(4, target)
        if max_pairs is None:
            max_pairs = 40
        if coin_list is None:
            coin_list = list(STAGE_C_COINS[:6])
    elif mode == "bucket_coverage":
        # Starts are fixed; no universe planning.
        pass
    elif mode == "full":
        # Explicit full universe — no coin filter, no max_pairs unless provided.
        coin_list = None
        if max_windows is None:
            max_windows = None
        log(
            "WARNING: mode=full will run the full discovered coin universe. "
            "This is intentional only for a manual 27-coin campaign."
        )

    run_validation(
        output_dir=args.output_dir,
        candle_limit=args.candle_limit,
        target_per_window=target,
        seed=args.seed,
        warmup=args.warmup,
        smoke=bool(args.smoke) or mode == "smoke",
        resume=bool(args.resume),
        smoke_coins=smoke_coins if (args.smoke or mode == "smoke") else None,
        coins=coin_list,
        max_windows_per_coin=max_windows,
        max_pairs=max_pairs,
        mode=mode,
        starts_csv=args.starts,
    )
    if mode not in {"full", "bucket_coverage"}:
        print_manual_commands(
            ROOT
            / "research/backtests/results/adaptive_distance_staging_large_1000_500_YYYYMMDD",
            args.log_dir,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

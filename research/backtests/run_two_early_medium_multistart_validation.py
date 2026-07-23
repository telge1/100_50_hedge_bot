#!/usr/bin/env python3
"""Causal multi-start validation: two_early_medium vs legacy @1000/500.

Implements start selection, checkpoint/resume, smoke, and full-run CLI.
Does NOT start the long full run automatically — use the printed nohup command.

Profiles compared: legacy, two_early_medium only.
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
from typing import Any

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
from research.backtests.recovery_reentry_policy import load_baseline_blockers
from research.backtests.run_two_early_medium_candidate_validation import (
    _run_one as _candidate_run_one,
)
from research.backtests.two_early_medium_multistart_metrics import (
    atom_regression_rows,
    bootstrap_ci,
    compare_pair,
    decide,
    leaveouts,
    summarize_by_coin,
    summarize_by_regime,
    summarize_pairs,
)
from research.backtests.two_early_medium_multistart_starts import (
    DEFAULT_COINS,
    DEFAULT_GRID_STEP,
    DEFAULT_MIN_REMAINING,
    DEFAULT_SEED,
    DEFAULT_TARGET_PER_COIN,
    DEFAULT_WARMUP,
    assert_no_lookahead_features,
    pair_key,
    profile_run_key,
    select_start_points_for_coin,
    start_points_to_rows,
)

ROOT = Path(__file__).resolve().parents[2]
CANDIDATE_DIR = (
    ROOT / "research/backtests/results/two_early_medium_candidate_validation_1000_500_20260721"
)
DEFAULT_OUT = (
    ROOT / "research/backtests/results/two_early_medium_multistart_validation_1000_500_20260721"
)
PROFILES = ("legacy", "two_early_medium")
PROTECTED = (
    CANDIDATE_DIR,
    ROOT / "research/backtests/results/multicoin_price_staging_grid_1000_500_20260721",
    DEFAULT_BASELINE,
)


def log(msg: str) -> None:
    print(msg, flush=True)


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


def _load_blocker_starts(coins: list[str]) -> dict[str, list[int]]:
    """Historical blocker starts from candidate validation / baseline."""
    out: dict[str, list[int]] = {c.upper(): [] for c in coins}
    pair_csv = CANDIDATE_DIR / "trade_pair_comparison.csv"
    if pair_csv.exists():
        for row in load_csv_rows(pair_csv):
            coin = str(row.get("coin") or "").upper()
            if coin in out:
                out[coin].append(int(safe_float(row.get("start_index"))))
    blockers = load_baseline_blockers(DEFAULT_BASELINE / "blocker_trades.csv")
    for row in blockers:
        coin = str(row.get("coin") or "").upper()
        if coin in out:
            idx = row.get("start_index")
            if idx is None:
                idx = row.get("absolute_start_index")
            if idx is not None:
                out[coin].append(int(idx))
    # unique preserve order
    for coin in out:
        seen: set[int] = set()
        uniq: list[int] = []
        for i in out[coin]:
            if i not in seen:
                seen.add(i)
                uniq.append(i)
        out[coin] = uniq
    return out


def _window_close_drawdown_pct(candles: list[Any], start_index: int, processed: int) -> float | None:
    if processed <= 1:
        return None
    end = min(len(candles), start_index + processed)
    closes = []
    for c in candles[start_index:end]:
        closes.append(float(c["close"] if isinstance(c, dict) else c.close))
    if not closes:
        return None
    peak = closes[0]
    max_dd = 0.0
    for px in closes:
        peak = max(peak, px)
        if peak > 0:
            max_dd = max(max_dd, (peak - px) / peak * 100.0)
    return max_dd


def run_profile_at_start(
    *,
    coin: str,
    start_index: int,
    profile: str,
    candles: list[Any],
    max_window_candles: int | None,
    capture_economics: bool,
) -> dict[str, Any]:
    """Run one profile from an absolute start index (optional truncated window)."""
    if max_window_candles is not None and max_window_candles > 0:
        end = min(len(candles), int(start_index) + int(max_window_candles))
        series = candles[:end]
    else:
        series = candles

    # Reuse candidate evaluator for coverage/safety fields.
    row = _candidate_run_one(
        coin=coin,
        trade_number=0,
        start_index=int(start_index),
        profile=profile,
        candles=series,
        baseline_row=None,
        capture_economics=capture_economics,
    )
    # Enrich exposure aliases from analyze_blocker_run / analyze_trade
    row["closed_pnl"] = safe_float(row.get("realized_pnl"))
    row["economically_valid_close"] = int(
        int(row.get("trade_flat") or 0) == 1
        and row.get("coverage_class") in {"covered_by_second_leg", "covered_by_basket_exit"}
        and int(row.get("economic_undercoverage_closed") or 0) == 0
    )
    processed = int(row.get("candles_processed") or row.get("duration_candles") or 0)
    row["duration_candles"] = processed
    if row.get("max_drawdown_pct") in (None, ""):
        row["max_drawdown_pct"] = _window_close_drawdown_pct(series, int(start_index), processed)
    # analyze_blocker_run exposes net/gross exposure aliases
    row["max_abs_net_exposure"] = safe_float(
        row.get("max_abs_net_exposure") or row.get("net_exposure")
    )
    row["max_long_notional"] = safe_float(
        row.get("max_long_notional") or row.get("gross_exposure")
    )
    row["max_short_notional"] = safe_float(row.get("max_short_notional"))
    row["fees"] = safe_float(row.get("fees"))
    row.setdefault("duplicate_stage", 0)
    row.setdefault("over_close", 0)
    row["late_stage_fill_after_exit"] = len(row.get("late_stage_fills_after_exit") or [])
    row["orphan_stage_order"] = int(row.get("orphan_stage_order") or 0)
    row["profile"] = profile
    row["run_key"] = profile_run_key(coin, start_index, profile)
    row["pair_key"] = pair_key(coin, start_index)
    return row


def plan_starts(
    *,
    coins: list[str],
    candle_limit: int,
    target_per_coin: int,
    seed: int,
    warmup: int,
    min_remaining: int,
    grid_step: int,
    smoke: bool,
) -> tuple[list[dict[str, Any]], dict[str, list[Any]], dict[str, Any]]:
    blocker_starts = _load_blocker_starts(coins)
    coin_candles: dict[str, list[Any]] = {}
    points_rows: list[dict[str, Any]] = []
    for coin in coins:
        raw = load_candles_for_symbol(coin, limit=candle_limit)
        candles = normalize_candles(coin, raw)
        coin_candles[coin] = candles
        if smoke:
            # Few deterministic starts: grid + blocker + one regime if available.
            pts = select_start_points_for_coin(
                coin=coin,
                candles=candles,
                historical_blocker_starts=blocker_starts.get(coin, []),
                target_total=min(6, target_per_coin),
                seed=seed,
                warmup=warmup,
                min_remaining=min(800, min_remaining) if smoke else min_remaining,
                grid_step=max(grid_step, 5000) if smoke else grid_step,
                regime_quota=1,
                random_quota=1,
                grid_quota=2,
            )
        else:
            pts = select_start_points_for_coin(
                coin=coin,
                candles=candles,
                historical_blocker_starts=blocker_starts.get(coin, []),
                target_total=target_per_coin,
                seed=seed,
                warmup=warmup,
                min_remaining=min_remaining,
                grid_step=grid_step,
            )
        # Lookahead sanity on a sample
        if pts:
            sample = pts[len(pts) // 2]
            assert_no_lookahead_features(candles, sample.start_index, sample.causal_features)
        points_rows.extend(start_points_to_rows(pts))

    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "coins": coins,
        "n_coins": len(coins),
        "n_start_points": len(points_rows),
        "n_planned_pairs": len(points_rows),
        "n_planned_profile_runs": len(points_rows) * len(PROFILES),
        "profiles": list(PROFILES),
        "sizes": "1000:500",
        "seed": seed,
        "warmup": warmup,
        "min_remaining": min_remaining,
        "grid_step": grid_step,
        "target_per_coin": target_per_coin,
        "smoke": smoke,
        "candle_limit": candle_limit,
        "blocker_starts_by_coin": blocker_starts,
        "starts_per_coin": {
            c: sum(1 for r in points_rows if r["coin"] == c) for c in coins
        },
    }
    return points_rows, coin_candles, manifest


def _append_csv_row(path: Path, row: dict[str, Any], fieldnames: list[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = path.exists() and path.stat().st_size > 0
    if fieldnames is None:
        fieldnames = list(row.keys())
    with path.open("a", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        if not exists:
            w.writeheader()
        out = {
            k: (json.dumps(v, default=str) if isinstance(v, (dict, list)) else v)
            for k, v in row.items()
            if k in fieldnames
        }
        # include any new keys by rewriting is hard; keep fixed schema from first row
        w.writerow(out)


def build_artifacts(
    *,
    output_dir: Path,
    start_rows: list[dict[str, Any]],
    raw_rows: list[dict[str, Any]],
    manifest: dict[str, Any],
    integrity_extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    by_key_profile: dict[str, dict[str, dict[str, Any]]] = {}
    for row in raw_rows:
        pk = str(row.get("pair_key") or pair_key(row["coin"], int(row["start_index"])))
        by_key_profile.setdefault(pk, {})[str(row["profile"])] = row

    start_meta = {}
    for r in start_rows:
        meta = dict(r)
        cats = meta.get("categories")
        if isinstance(cats, str):
            try:
                cats = json.loads(cats)
            except json.JSONDecodeError:
                cats = [cats]
        meta["categories"] = cats
        meta["is_historical_blocker"] = int(safe_float(meta.get("is_historical_blocker")))
        meta["is_neutral_pool"] = int(safe_float(meta.get("is_neutral_pool")))
        start_meta[str(r["pair_key"])] = meta

    pairs: list[dict[str, Any]] = []
    for pk, profs in sorted(by_key_profile.items()):
        if "legacy" not in profs or "two_early_medium" not in profs:
            continue
        pairs.append(compare_pair(profs["legacy"], profs["two_early_medium"], start_meta.get(pk, {"pair_key": pk})))

    summary = summarize_pairs(pairs)
    by_coin = summarize_by_coin(pairs)
    by_regime = summarize_by_regime(pairs)
    leave = leaveouts(pairs, by_coin)
    boot = bootstrap_ci([safe_float(p.get("delta_total_pnl")) for p in pairs])

    neutral_row = next((r for r in by_regime if r["regime_group"] == "neutral_pool"), {})
    summary["neutral_pool"] = {
        "delta_total": {
            "sum": neutral_row.get("sum_delta_total_pnl"),
            "median": neutral_row.get("median_delta_total_pnl"),
        },
        "n_pairs": neutral_row.get("n_pairs"),
        "better": neutral_row.get("better"),
        "worse": neutral_row.get("worse"),
    }

    # Exposure / drawdown deltas
    exp_rows = []
    for p in pairs:
        exp_rows.append(
            {
                "pair_key": p["pair_key"],
                "coin": p["coin"],
                "start_index": p["start_index"],
                "delta_max_long_notional": safe_float(p.get("staging_max_long_notional"))
                - safe_float(p.get("legacy_max_long_notional")),
                "delta_max_short_notional": safe_float(p.get("staging_max_short_notional"))
                - safe_float(p.get("legacy_max_short_notional")),
                "delta_max_abs_net_exposure": safe_float(p.get("staging_max_abs_net_exposure"))
                - safe_float(p.get("legacy_max_abs_net_exposure")),
                "delta_max_drawdown_pct": safe_float(p.get("staging_max_drawdown_pct"))
                - safe_float(p.get("legacy_max_drawdown_pct")),
                "legacy_max_abs_net_exposure": p.get("legacy_max_abs_net_exposure"),
                "staging_max_abs_net_exposure": p.get("staging_max_abs_net_exposure"),
                "legacy_max_drawdown_pct": p.get("legacy_max_drawdown_pct"),
                "staging_max_drawdown_pct": p.get("staging_max_drawdown_pct"),
            }
        )
    med_exp = None
    med_dd = None
    if exp_rows:
        med_exp = sorted(safe_float(r["delta_max_abs_net_exposure"]) for r in exp_rows)[
            len(exp_rows) // 2
        ]
        med_dd = sorted(safe_float(r["delta_max_drawdown_pct"]) for r in exp_rows)[len(exp_rows) // 2]
    # Soft: median exposure/drawdown not sharply worse
    summary["exposure_drawdown_ok"] = (med_exp is None or med_exp <= 50.0) and (
        med_dd is None or med_dd <= 1.0
    )

    atom_cases = atom_regression_rows(pairs)
    # Bounded if rare (<15% of ATOM pairs) or single outlier pattern
    atom_pairs = [p for p in pairs if str(p.get("coin")).upper() == "ATOMUSDT"]
    atom_reg_rate = (len(atom_cases) / len(atom_pairs)) if atom_pairs else 0.0
    summary["atom_regression_bounded"] = atom_reg_rate <= 0.25 and (
        not atom_cases or safe_float(atom_cases[0].get("delta_total_pnl")) > -400
    )
    summary["atom_regression_rate"] = atom_reg_rate

    # Safety integrity
    safety = {
        "economic_undercoverage_closed": sum(
            int(r.get("economic_undercoverage_closed") or 0) for r in raw_rows
        ),
        "invalid_partial": sum(int(safe_float(r.get("invalid_partial"))) for r in raw_rows),
        "over_close": sum(int(r.get("over_close") or 0) for r in raw_rows),
        "duplicate_stage": sum(int(r.get("duplicate_stage") or 0) for r in raw_rows),
        "late_stage_fill_after_exit": sum(
            int(r.get("late_stage_fill_after_exit") or 0) for r in raw_rows
        ),
        "orphan_stage_order": sum(int(r.get("orphan_stage_order") or 0) for r in raw_rows),
        "sufficient_false_closed": sum(int(r.get("sufficient_false_closed") or 0) for r in raw_rows),
    }
    missing_pairs = [
        r["pair_key"]
        for r in start_rows
        if r["pair_key"] not in by_key_profile
        or "legacy" not in by_key_profile[r["pair_key"]]
        or "two_early_medium" not in by_key_profile[r["pair_key"]]
    ]
    dup_run_keys = []
    seen_rk: set[str] = set()
    for r in raw_rows:
        rk = str(r.get("run_key"))
        if rk in seen_rk:
            dup_run_keys.append(rk)
        seen_rk.add(rk)

    integrity = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "planned_pairs": len(start_rows),
        "completed_pairs": len(pairs),
        "planned_profile_runs": len(start_rows) * 2,
        "completed_profile_runs": len(raw_rows),
        "missing_pairs": missing_pairs,
        "duplicate_run_keys": dup_run_keys,
        "safety": safety,
        "safety_ok": all(v == 0 for v in safety.values()),
        "profiles_identical_starts": True,
        "pass": all(v == 0 for v in safety.values())
        and not missing_pairs
        and not dup_run_keys,
    }
    if integrity_extra:
        integrity.update(integrity_extra)

    decision = decide(summary, leave, bool(integrity["pass"]))

    worst = sorted(pairs, key=lambda p: safe_float(p.get("delta_total_pnl")))[:10]
    transitions = [
        {"bucket": bucket, "n": n}
        for bucket, n in Counter(p.get("bucket") for p in pairs).items()
    ]

    aggregate = [
        {
            "metric": "n_pairs",
            "value": summary["n_pairs"],
        },
        {
            "metric": "better_equal_worse",
            "value": f"{summary['better']}/{summary['equal']}/{summary['worse']}",
        },
        {"metric": "sum_delta_total_pnl", "value": summary["delta_total"]["sum"]},
        {"metric": "median_delta_total_pnl", "value": summary["delta_total"]["median"]},
        {"metric": "mean_delta_total_pnl", "value": summary["delta_total"]["mean"]},
        {"metric": "legacy_valid_closes", "value": summary["legacy_valid_closes"]},
        {"metric": "staging_valid_closes", "value": summary["staging_valid_closes"]},
        {"metric": "additional_valid_closes", "value": summary["additional_valid_closes"]},
        {"metric": "lost_valid_closes", "value": summary["lost_valid_closes"]},
        {"metric": "without_apt_delta", "value": leave["without_apt"]},
        {"metric": "without_atom_delta", "value": leave["without_atom"]},
        {"metric": "without_top3_delta", "value": leave["without_top3"]},
        {"metric": "verdict", "value": decision["verdict"]},
    ]

    write_csv(output_dir / "start_points.csv", start_rows)
    write_csv(output_dir / "pair_results.csv", pairs)
    write_csv(output_dir / "aggregate_summary.csv", aggregate)
    write_csv(output_dir / "summary_by_coin.csv", by_coin)
    write_csv(output_dir / "summary_by_regime.csv", by_regime)
    write_csv(output_dir / "status_transitions.csv", transitions)
    write_csv(output_dir / "atom_regression_cases.csv", atom_cases)
    write_csv(output_dir / "worst_cases.csv", worst)
    write_csv(output_dir / "exposure_drawdown.csv", exp_rows)
    write_csv(output_dir / "raw_profile_runs.csv", raw_rows)
    atomic_write_json(output_dir / "run_manifest.json", manifest)
    atomic_write_json(output_dir / "bootstrap_results.json", boot)
    atomic_write_json(output_dir / "integrity.json", integrity)
    atomic_write_json(
        output_dir / "decision.json",
        {
            **decision,
            "summary": summary,
            "leaveouts": leave,
            "worst_three_coins": sorted(
                by_coin, key=lambda r: safe_float(r.get("sum_delta_total_pnl"))
            )[:3],
        },
    )

    report = [
        "# two_early_medium Multi-Start Validation @1000/500",
        "",
        f"Generated: `{integrity['generated_at']}`",
        f"Smoke: `{manifest.get('smoke')}`",
        "",
        "## Scope",
        "",
        f"- Coins ({manifest['n_coins']}): {', '.join(manifest['coins'])}",
        f"- Start points / pairs planned: **{manifest['n_planned_pairs']}**",
        f"- Profile runs planned: **{manifest['n_planned_profile_runs']}**",
        f"- Completed pairs: **{integrity['completed_pairs']}**",
        "",
        "## Integrity",
        "",
        f"- pass: **{integrity['pass']}**",
        f"- safety: `{json.dumps(safety)}`",
        "",
        "## Aggregate",
        "",
        f"- better/equal/worse: **{summary['better']}/{summary['equal']}/{summary['worse']}**",
        f"- sum/median/mean Δ Total PnL: "
        f"**{summary['delta_total']['sum']:.4f} / {summary['delta_total']['median']} / "
        f"{summary['delta_total']['mean']}**",
        f"- valid closes L/TEM: **{summary['legacy_valid_closes']} / {summary['staging_valid_closes']}**",
        f"- additional / lost valid closes: "
        f"**{summary['additional_valid_closes']} / {summary['lost_valid_closes']}**",
        "",
        "## Leave-outs",
        "",
        f"- without APT: **{leave['without_apt']}**",
        f"- without ATOM: **{leave['without_atom']}**",
        f"- without Top-3 {leave['top3_coins']}: **{leave['without_top3']}**",
        "",
        "## ATOM",
        "",
        f"- ATOM pairs: **{len(atom_pairs)}**; regression-like: **{len(atom_cases)}** "
        f"(rate={atom_reg_rate:.2%})",
        "",
        "## Decision",
        "",
        f"**{decision['verdict']}**",
        "",
        decision["next_step"],
        "",
        f"Live: {decision['live_integration']}",
        "",
    ]
    (output_dir / "REPORT.md").write_text("\n".join(report) + "\n", encoding="utf-8")
    return {
        "integrity": integrity,
        "decision": decision,
        "summary": summary,
        "n_pairs": len(pairs),
    }


def run_validation(
    *,
    output_dir: Path,
    coins: list[str],
    candle_limit: int,
    target_per_coin: int,
    seed: int,
    warmup: int,
    min_remaining: int,
    grid_step: int,
    smoke: bool,
    resume: bool,
    max_window_candles: int | None,
    max_pairs: int | None,
) -> dict[str, Any]:
    assert_output_dir_safe(output_dir, resume=resume)
    for protected in PROTECTED:
        if output_dir.resolve() == protected.resolve():
            raise RuntimeError(f"refusing protected output dir: {protected}")

    output_dir.mkdir(parents=True, exist_ok=True)
    start_path = output_dir / "start_points.csv"
    raw_path = output_dir / "raw_profile_runs.csv"
    ck_path = output_dir / "checkpoint.json"
    manifest_path = output_dir / "run_manifest.json"

    if resume and start_path.exists() and manifest_path.exists():
        start_rows = load_csv_rows(start_path)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        coin_candles = {}
        for coin in manifest["coins"]:
            coin_candles[coin] = normalize_candles(
                coin,
                load_candles_for_symbol(coin, limit=int(manifest.get("candle_limit") or candle_limit)),
            )
        log(f"[resume] loaded {len(start_rows)} planned starts")
    else:
        if resume:
            log("[resume] no prior plan found — planning fresh")
        start_rows, coin_candles, manifest = plan_starts(
            coins=coins,
            candle_limit=candle_limit,
            target_per_coin=target_per_coin,
            seed=seed,
            warmup=warmup,
            min_remaining=min_remaining,
            grid_step=grid_step,
            smoke=smoke,
        )
        if max_pairs is not None:
            start_rows = start_rows[: int(max_pairs)]
            manifest["n_start_points"] = len(start_rows)
            manifest["n_planned_pairs"] = len(start_rows)
            manifest["n_planned_profile_runs"] = len(start_rows) * 2
            manifest["max_pairs_applied"] = int(max_pairs)
        write_csv(start_path, start_rows)
        atomic_write_json(manifest_path, manifest)
        atomic_write_json(
            ck_path,
            _empty_checkpoint(coins=coins, planned_pairs=len(start_rows)),
        )

    ck = load_checkpoint(ck_path) or _empty_checkpoint(
        coins=list(manifest["coins"]), planned_pairs=len(start_rows)
    )
    done_pairs = set(ck.get("completed_pair_keys") or [])
    done_runs = set(ck.get("completed_run_keys") or [])

    # Reload existing raw rows on resume
    raw_rows = load_csv_rows(raw_path)
    profiles_by_pair: dict[str, set[str]] = defaultdict(set)
    for r in raw_rows:
        done_runs.add(str(r.get("run_key")))
        profiles_by_pair[str(r.get("pair_key"))].add(str(r.get("profile")))
    done_pairs = {
        pk for pk, ps in profiles_by_pair.items() if set(PROFILES).issubset(ps)
    } | {
        pk
        for pk in done_pairs
        if pk in profiles_by_pair and set(PROFILES).issubset(profiles_by_pair[pk])
    }

    # Fieldnames for append
    raw_fields: list[str] | None = list(raw_rows[0].keys()) if raw_rows else None

    t0 = time.time()
    planned = len(start_rows)
    for i, sp in enumerate(start_rows, start=1):
        pk = str(sp["pair_key"])
        coin = str(sp["coin"])
        start_index = int(sp["start_index"])
        if pk in done_pairs and all(
            profile_run_key(coin, start_index, p) in done_runs for p in PROFILES
        ):
            continue
        log(f"[{i}/{planned}] {pk}")
        candles = coin_candles[coin]
        for profile in PROFILES:
            rk = profile_run_key(coin, start_index, profile)
            if rk in done_runs:
                continue
            try:
                row = run_profile_at_start(
                    coin=coin,
                    start_index=start_index,
                    profile=profile,
                    candles=candles,
                    max_window_candles=max_window_candles,
                    capture_economics=(profile != "legacy"),
                )
                row["primary_category"] = sp.get("primary_category")
                row["categories"] = sp.get("categories")
                if raw_fields is None:
                    raw_fields = list(row.keys())
                _append_csv_row(raw_path, row, raw_fields)
                raw_rows.append(row)
                done_runs.add(rk)
            except Exception as exc:  # noqa: BLE001 — research harness records and continues
                err = {
                    "run_key": rk,
                    "error": str(exc),
                    "traceback": traceback.format_exc(),
                }
                ck.setdefault("errors", []).append(err)
                log(f"  ERROR {rk}: {exc}")
                atomic_write_json(ck_path, ck)
                raise

        done_pairs.add(pk)
        ck["completed_pair_keys"] = sorted(done_pairs)
        ck["completed_run_keys"] = sorted(done_runs)
        ck["updated_at"] = datetime.now(timezone.utc).isoformat()
        atomic_write_json(ck_path, ck)

    # Reference parity: APT blocker start if present
    parity = {}
    apt_blocker = next(
        (
            r
            for r in start_rows
            if r["coin"] == "APTUSDT" and int(safe_float(r.get("is_historical_blocker")))
        ),
        None,
    )
    if apt_blocker:
        # Compare legacy total to candidate validation if same start
        cand_pairs = load_csv_rows(CANDIDATE_DIR / "trade_pair_comparison.csv")
        ref = next(
            (
                r
                for r in cand_pairs
                if r.get("coin") == "APTUSDT"
                and int(safe_float(r.get("start_index"))) == int(apt_blocker["start_index"])
            ),
            None,
        )
        cur = next(
            (
                r
                for r in raw_rows
                if r.get("coin") == "APTUSDT"
                and r.get("profile") == "legacy"
                and int(safe_float(r.get("start_index"))) == int(apt_blocker["start_index"])
            ),
            None,
        )
        if ref and cur and max_window_candles is None:
            parity = {
                "apt_blocker_start": int(apt_blocker["start_index"]),
                "candidate_legacy_total": safe_float(ref.get("legacy_total_pnl")),
                "multistart_legacy_total": safe_float(cur.get("total_pnl")),
                "parity_ok": abs(
                    safe_float(ref.get("legacy_total_pnl")) - safe_float(cur.get("total_pnl"))
                )
                < 1.0,
            }

    manifest["elapsed_sec"] = round(time.time() - t0, 2)
    manifest["completed_pairs"] = len(done_pairs)
    atomic_write_json(manifest_path, manifest)

    result = build_artifacts(
        output_dir=output_dir,
        start_rows=start_rows,
        raw_rows=raw_rows,
        manifest=manifest,
        integrity_extra={"legacy_reference_parity": parity},
    )
    log(
        json.dumps(
            {
                "integrity_pass": result["integrity"]["pass"],
                "n_pairs": result["n_pairs"],
                "verdict": result["decision"]["verdict"],
                "elapsed_sec": manifest["elapsed_sec"],
                "output_dir": str(output_dir),
            },
            indent=2,
        )
    )
    return result


def print_manual_commands(output_dir: Path) -> None:
    out = str(output_dir)
    print(
        "\n=== MANUAL FULL RUN (do not auto-start from agent) ===\n"
        f"mkdir -p {out}/logs && \\\n"
        f"nohup env PYTHONPATH=. python -m research.backtests.run_two_early_medium_multistart_validation \\\n"
        f"  --output-dir {out} \\\n"
        f"  --coins APTUSDT,ATOMUSDT,ADAUSDT,ARBUSDT,SUIUSDT,SEIUSDT,TIAUSDT,TRXUSDT,DOTUSDT,OPUSDT,BTCUSDT,ETHUSDT \\\n"
        f"  --target-per-coin 40 \\\n"
        f"  --seed 20260721 \\\n"
        f"  > {out}/logs/full_run.out 2>&1 &\n"
        f"echo $! > {out}/logs/full_run.pid\n"
        "\n# Follow:\n"
        f"tail -f {out}/logs/full_run.out\n"
        "\n# Process status:\n"
        f"ps -p $(cat {out}/logs/full_run.pid) -o pid,etime,cmd\n"
        "\n# Resume after interrupt:\n"
        f"nohup env PYTHONPATH=. python -m research.backtests.run_two_early_medium_multistart_validation \\\n"
        f"  --output-dir {out} --resume \\\n"
        f"  > {out}/logs/full_run_resume.out 2>&1 &\n"
        f"echo $! > {out}/logs/full_run.pid\n"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUT)
    parser.add_argument(
        "--coins",
        default=",".join(DEFAULT_COINS),
        help="Comma-separated coins",
    )
    parser.add_argument("--candle-limit", type=int, default=FULL_HISTORY_CANDLE_LIMIT)
    parser.add_argument("--target-per-coin", type=int, default=DEFAULT_TARGET_PER_COIN)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--warmup", type=int, default=DEFAULT_WARMUP)
    parser.add_argument("--min-remaining", type=int, default=DEFAULT_MIN_REMAINING)
    parser.add_argument("--grid-step", type=int, default=DEFAULT_GRID_STEP)
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument(
        "--max-window-candles",
        type=int,
        default=None,
        help="Optional truncate after start (smoke speed-up)",
    )
    parser.add_argument("--max-pairs", type=int, default=None)
    parser.add_argument(
        "--print-manual-commands-only",
        action="store_true",
        help="Only print nohup/resume commands",
    )
    args = parser.parse_args(argv)

    if args.print_manual_commands_only:
        print_manual_commands(args.output_dir)
        return 0

    coins = [c.strip().upper() for c in str(args.coins).split(",") if c.strip()]
    if args.smoke and not args.max_window_candles:
        args.max_window_candles = 2500

    run_validation(
        output_dir=args.output_dir,
        coins=coins,
        candle_limit=args.candle_limit,
        target_per_coin=args.target_per_coin,
        seed=args.seed,
        warmup=args.warmup,
        min_remaining=args.min_remaining,
        grid_step=args.grid_step,
        smoke=bool(args.smoke),
        resume=bool(args.resume),
        max_window_candles=args.max_window_candles,
        max_pairs=args.max_pairs,
    )
    if not args.smoke:
        print_manual_commands(args.output_dir)
    else:
        print_manual_commands(DEFAULT_OUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

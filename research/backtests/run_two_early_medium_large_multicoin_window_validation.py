#!/usr/bin/env python3
"""Large multi-coin × time-window validation: two_early_medium vs legacy @1000/500.

Does NOT start the full run automatically. Use the printed nohup command
(logs/PID outside the output directory).
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
from research.backtests.run_two_early_medium_multistart_validation import (
    _load_blocker_starts,
    run_profile_at_start,
)
from research.backtests.two_early_medium_multistart_metrics import bootstrap_ci, summarize_pairs
from research.backtests.two_early_medium_multistart_starts import DEFAULT_SEED, DEFAULT_WARMUP
from research.backtests.two_early_medium_window_metrics import (
    compare_window_pair,
    decide_large,
    leaveout_analysis,
    open_mtm_followthrough_stub_rows,
    summarize_by_keys,
    summarize_by_start_category,
)
from research.backtests.two_early_medium_window_plan import (
    MIN_WINDOW_STARTS,
    TARGET_STARTS_PER_WINDOW,
    build_time_windows_for_coin,
    discover_coin_universe,
    select_starts_for_window,
    window_pair_key,
    window_profile_run_key,
    windows_to_rows,
)

ROOT = Path(__file__).resolve().parents[2]
PRIOR_MS = (
    ROOT / "research/backtests/results/two_early_medium_multistart_validation_1000_500_20260721"
)
DEFAULT_OUT = (
    ROOT
    / "research/backtests/results/two_early_medium_large_multicoin_window_validation_1000_500_20260721"
)
# Logs/PID live outside output dir so safety guards stay clean.
DEFAULT_LOG_DIR = ROOT / "research/backtests/results/_logs"
PROFILES = ("legacy", "two_early_medium")
PROTECTED = (
    PRIOR_MS,
    ROOT / "research/backtests/results/two_early_medium_candidate_validation_1000_500_20260721",
    ROOT / "research/backtests/results/multicoin_price_staging_grid_1000_500_20260721",
    DEFAULT_BASELINE,
)
FOLLOWTHROUGH_BARS = 3000


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
        "completed_coin_windows": [],
        "errors": [],
        "updated_at": None,
    }


def plan_universe_and_starts(
    *,
    candle_limit: int | None,
    target_per_window: int,
    seed: int,
    warmup: int,
    smoke: bool,
    smoke_coins: list[str] | None,
    max_windows_per_coin: int | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], dict[str, list[Any]], dict[str, Any]]:
    included, excluded = discover_coin_universe(candle_limit=candle_limit)
    if smoke and smoke_coins:
        want = {c.upper() for c in smoke_coins}
        included = [r for r in included if r["coin"] in want]
        # If discovery excluded a smoke coin, still try load
        have = {r["coin"] for r in included}
        for c in want - have:
            raw = load_candles_for_symbol(c, limit=candle_limit)
            candles = normalize_candles(c, raw)
            included.append(
                {
                    "coin": c,
                    "n_candles": len(candles),
                    "first_timestamp": None,
                    "last_timestamp": None,
                    "source": "smoke_forced",
                    "included": True,
                }
            )

    coins = [r["coin"] for r in included]
    blockers = _load_blocker_starts(coins)
    coin_candles: dict[str, list[Any]] = {}
    window_rows: list[dict[str, Any]] = []
    start_rows: list[dict[str, Any]] = []

    for coin in coins:
        raw = load_candles_for_symbol(coin, limit=candle_limit)
        candles = normalize_candles(coin, raw)
        coin_candles[coin] = candles
        windows = build_time_windows_for_coin(coin, candles, warmup=warmup)
        if smoke:
            # Prefer early + late (or first two non-full)
            chron = [w for w in windows if w.kind in {"early", "middle", "late", "recent"}]
            windows = chron[:2] if chron else windows[:2]
        if max_windows_per_coin is not None:
            windows = windows[: int(max_windows_per_coin)]
        window_rows.extend(windows_to_rows(windows))
        for w in windows:
            tgt = min(4, target_per_window) if smoke else target_per_window
            rows = select_starts_for_window(
                coin=coin,
                candles=candles,
                window=w,
                blocker_starts=blockers.get(coin, []),
                target_starts=tgt,
                seed=seed,
                warmup=warmup,
                smoke=smoke,
            )
            start_rows.extend(rows)
            log(f"[plan] {coin}/{w.window_id}: {len(rows)} starts")

    # Deduplicate pair keys
    seen: set[str] = set()
    uniq_starts: list[dict[str, Any]] = []
    for r in start_rows:
        pk = r["pair_key"]
        if pk in seen:
            continue
        seen.add(pk)
        uniq_starts.append(r)

    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "profiles": list(PROFILES),
        "sizes": "1000:500",
        "seed": seed,
        "warmup": warmup,
        "smoke": smoke,
        "candle_limit": candle_limit,
        "target_per_window": target_per_window,
        "coins": coins,
        "n_coins": len(coins),
        "n_excluded": len(excluded),
        "n_windows_rows": len(window_rows),
        "n_planned_pairs": len(uniq_starts),
        "n_planned_profile_runs": len(uniq_starts) * 2,
        "starts_per_coin_window": dict(
            Counter((r["coin"], r["window_id"]) for r in uniq_starts)
        ),
    }
    # JSON-safe counter keys
    manifest["starts_per_coin_window"] = {
        f"{a}|{b}": n for (a, b), n in Counter((r["coin"], r["window_id"]) for r in uniq_starts).items()
    }
    return included, excluded, window_rows, uniq_starts, coin_candles, manifest


def build_artifacts(
    *,
    output_dir: Path,
    included: list[dict[str, Any]],
    excluded: list[dict[str, Any]],
    window_rows: list[dict[str, Any]],
    start_rows: list[dict[str, Any]],
    raw_rows: list[dict[str, Any]],
    manifest: dict[str, Any],
    followthrough_rows: list[dict[str, Any]],
) -> dict[str, Any]:
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

    by_key: dict[str, dict[str, dict[str, Any]]] = {}
    for row in raw_rows:
        pk = str(row.get("pair_key"))
        by_key.setdefault(pk, {})[str(row["profile"])] = row

    pairs: list[dict[str, Any]] = []
    for pk, profs in sorted(by_key.items()):
        if "legacy" not in profs or "two_early_medium" not in profs:
            continue
        pairs.append(compare_window_pair(profs["legacy"], profs["two_early_medium"], start_meta.get(pk, {"pair_key": pk})))

    summary = summarize_pairs(pairs)
    by_coin = summarize_by_keys(pairs, ["coin"])
    by_window = summarize_by_keys(pairs, ["window_id"])
    by_coin_window = summarize_by_keys(pairs, ["coin", "window_id"])
    by_regime = summarize_by_keys(pairs, ["primary_category"])  # coarse
    by_start_cat = summarize_by_start_category(pairs)
    leave = leaveout_analysis(pairs)
    boot = bootstrap_ci([safe_float(p.get("delta_total_pnl")) for p in pairs])

    summary["by_window_positive_count"] = sum(
        1 for r in by_window if safe_float(r.get("sum_delta_total_pnl")) > 0
    )
    # Exposure soft check
    exp_rows = []
    for p in pairs:
        exp_rows.append(
            {
                "pair_key": p.get("pair_key"),
                "coin": p.get("coin"),
                "window_id": p.get("window_id"),
                "delta_max_abs_net_exposure": safe_float(p.get("staging_max_abs_net_exposure"))
                - safe_float(p.get("legacy_max_abs_net_exposure")),
                "delta_max_drawdown_pct": safe_float(p.get("staging_max_drawdown_pct"))
                - safe_float(p.get("legacy_max_drawdown_pct")),
                "delta_open_mtm": p.get("delta_open_mtm"),
                "delta_closed_pnl": p.get("delta_closed_pnl"),
            }
        )
    if exp_rows:
        med_exp = sorted(safe_float(r["delta_max_abs_net_exposure"]) for r in exp_rows)[
            len(exp_rows) // 2
        ]
        med_dd = sorted(safe_float(r["delta_max_drawdown_pct"]) for r in exp_rows)[len(exp_rows) // 2]
        summary["exposure_drawdown_ok"] = med_exp <= 80.0 and med_dd <= 1.5
    else:
        summary["exposure_drawdown_ok"] = True

    # Regression class: TRX/ATOM-like lost closes rate
    lost = [p for p in pairs if int(p.get("legacy_valid_close") or 0) == 1 and int(p.get("staging_valid_close") or 0) == 0]
    trx_like = [p for p in pairs if str(p.get("coin")) == "TRXUSDT" and p.get("better") == "staging_worse"]
    atom_like = [
        p
        for p in pairs
        if p.get("bucket") == "legacy_closed_staging_open"
    ]
    summary["regression_class_bounded"] = (len(lost) <= max(10, int(0.05 * max(len(pairs), 1)))) and (
        len(atom_like) <= max(15, int(0.08 * max(len(pairs), 1)))
    )

    additional = [
        p
        for p in pairs
        if int(p.get("legacy_valid_close") or 0) == 0 and int(p.get("staging_valid_close") or 0) == 1
    ]
    worst = sorted(pairs, key=lambda p: safe_float(p.get("delta_total_pnl")))[:25]
    transitions = [
        {"bucket": b, "n": n} for b, n in Counter(p.get("bucket") for p in pairs).items()
    ]

    safety = {
        "economic_undercoverage_closed": sum(int(r.get("economic_undercoverage_closed") or 0) for r in raw_rows),
        "invalid_partial": sum(int(safe_float(r.get("invalid_partial"))) for r in raw_rows),
        "over_close": sum(int(r.get("over_close") or 0) for r in raw_rows),
        "duplicate_stage": sum(int(r.get("duplicate_stage") or 0) for r in raw_rows),
        "late_stage_fill_after_exit": sum(int(r.get("late_stage_fill_after_exit") or 0) for r in raw_rows),
        "orphan_stage_order": sum(int(r.get("orphan_stage_order") or 0) for r in raw_rows),
        "sufficient_false_closed": sum(int(r.get("sufficient_false_closed") or 0) for r in raw_rows),
    }
    missing = [
        r["pair_key"]
        for r in start_rows
        if r["pair_key"] not in by_key
        or "legacy" not in by_key[r["pair_key"]]
        or "two_early_medium" not in by_key[r["pair_key"]]
    ]
    # Window boundary check
    oob = []
    for r in start_rows:
        lo = int(safe_float(r.get("start_index")))
        # find window row
        wmatch = [
            w
            for w in window_rows
            if w["coin"] == r["coin"] and w["window_id"] == r["window_id"]
        ]
        if not wmatch:
            continue
        w = wmatch[0]
        if not (int(w["start_index_lo"]) <= lo <= int(w["start_index_hi"])):
            oob.append(r["pair_key"])

    integrity = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "planned_pairs": len(start_rows),
        "completed_pairs": len(pairs),
        "missing_pairs": missing,
        "starts_outside_window": oob,
        "duplicate_pair_keys": len(start_rows) - len({r["pair_key"] for r in start_rows}),
        "safety": safety,
        "safety_ok": all(v == 0 for v in safety.values()),
        "pass": all(v == 0 for v in safety.values()) and not missing and not oob,
    }

    decision = decide_large(summary, leave, bool(integrity["pass"]))

    # Open MTM by window
    open_mtm_by_window = []
    for r in by_window:
        subset = [p for p in pairs if p.get("window_id") == r["window_id"]]
        open_mtm_by_window.append(
            {
                "window_id": r["window_id"],
                "sum_delta_open_mtm": sum(safe_float(p.get("delta_open_mtm")) for p in subset),
                "sum_delta_closed_pnl": sum(safe_float(p.get("delta_closed_pnl")) for p in subset),
                "sum_delta_total_pnl": r.get("sum_delta_total_pnl"),
            }
        )

    aggregate = [
        {"metric": "n_pairs", "value": summary["n_pairs"]},
        {"metric": "better_equal_worse", "value": f"{summary['better']}/{summary['equal']}/{summary['worse']}"},
        {"metric": "sum_delta_total_pnl", "value": summary["delta_total"]["sum"]},
        {"metric": "median_delta_total_pnl", "value": summary["delta_total"]["median"]},
        {"metric": "sum_delta_closed_pnl", "value": summary["sum_delta_closed_pnl"]},
        {"metric": "sum_delta_open_mtm", "value": summary["sum_delta_open_mtm"]},
        {"metric": "legacy_valid_closes", "value": summary["legacy_valid_closes"]},
        {"metric": "staging_valid_closes", "value": summary["staging_valid_closes"]},
        {"metric": "additional_valid_closes", "value": summary["additional_valid_closes"]},
        {"metric": "lost_valid_closes", "value": summary["lost_valid_closes"]},
        {"metric": "verdict", "value": decision["verdict"]},
    ]

    write_csv(output_dir / "coin_universe.csv", included)
    write_csv(output_dir / "excluded_coins.csv", excluded)
    write_csv(output_dir / "time_windows.csv", window_rows)
    write_csv(output_dir / "start_points.csv", start_rows)
    write_csv(output_dir / "pair_results.csv", pairs)
    write_csv(output_dir / "aggregate_summary.csv", aggregate)
    write_csv(output_dir / "summary_by_coin.csv", by_coin)
    write_csv(output_dir / "summary_by_window.csv", by_window)
    write_csv(output_dir / "summary_by_coin_window.csv", by_coin_window)
    write_csv(output_dir / "summary_by_regime.csv", by_regime)
    write_csv(output_dir / "summary_by_start_category.csv", by_start_cat)
    write_csv(output_dir / "status_transitions.csv", transitions)
    write_csv(output_dir / "lost_closes.csv", lost)
    write_csv(output_dir / "additional_closes.csv", additional)
    write_csv(output_dir / "worst_cases.csv", worst)
    write_csv(output_dir / "open_followthrough.csv", followthrough_rows)
    write_csv(output_dir / "exposure_drawdown.csv", exp_rows)
    write_csv(output_dir / "open_mtm_by_window.csv", open_mtm_by_window)
    write_csv(output_dir / "raw_profile_runs.csv", raw_rows)
    write_csv(output_dir / "atom_like_regressions.csv", atom_like)
    write_csv(output_dir / "trx_like_regressions.csv", trx_like)

    atomic_write_json(output_dir / "run_manifest.json", manifest)
    atomic_write_json(output_dir / "bootstrap_results.json", boot)
    atomic_write_json(output_dir / "leaveout_analysis.json", leave)
    atomic_write_json(output_dir / "integrity.json", integrity)
    atomic_write_json(
        output_dir / "decision.json",
        {**decision, "summary": summary, "leaveouts": leave, "open_mtm_by_window": open_mtm_by_window},
    )

    report = [
        "# two_early_medium Large Multi-Coin × Window Validation @1000/500",
        "",
        f"Generated: `{integrity['generated_at']}`",
        f"Smoke: `{manifest.get('smoke')}`",
        "",
        "## Scope",
        "",
        f"- Coins included: **{manifest['n_coins']}** (excluded {manifest['n_excluded']})",
        f"- Planned pairs: **{manifest['n_planned_pairs']}**",
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
        f"- Δ Total / Closed / OpenMTM: **{summary['delta_total']['sum']} / "
        f"{summary['sum_delta_closed_pnl']} / {summary['sum_delta_open_mtm']}**",
        f"- valid closes L/TEM: **{summary['legacy_valid_closes']} / {summary['staging_valid_closes']}**",
        f"- additional / lost: **{summary['additional_valid_closes']} / {summary['lost_valid_closes']}**",
        "",
        "## Leave-outs",
        "",
        f"- without APT: **{leave.get('without_apt')}**",
        f"- without Top-3 {leave.get('top3_coins')}: **{leave.get('without_top3')}**",
        f"- without best window {leave.get('best_window')}: **{leave.get('without_best_window')}**",
        f"- neutral pool: **{leave.get('neutral_pool_delta')}**",
        f"- regular/random: **{leave.get('regular_random_delta')}**",
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
    return {"integrity": integrity, "decision": decision, "summary": summary, "n_pairs": len(pairs)}


def run_followthrough(
    *,
    pairs: list[dict[str, Any]],
    start_meta: dict[str, dict[str, Any]],
    coin_candles: dict[str, list[Any]],
    smoke: bool,
) -> list[dict[str, Any]]:
    """Diagnostic extension past window end for open sides — separate from primary PnL."""
    stubs = open_mtm_followthrough_stub_rows(pairs)
    if smoke:
        stubs = stubs[:8]
    out: list[dict[str, Any]] = []
    for stub in stubs:
        pk = str(stub["pair_key"])
        meta = start_meta.get(pk) or {}
        coin = str(stub["coin"])
        start_index = int(stub["start_index"])
        run_end = int(meta.get("run_end_index") or 0)
        candles = coin_candles.get(coin) or []
        if not candles or run_end <= 0 or run_end >= len(candles):
            stub = {**stub, "followthrough_status": "no_future_data"}
            out.append(stub)
            continue
        ext_end = min(len(candles), run_end + FOLLOWTHROUGH_BARS)
        max_win = ext_end - start_index
        try:
            # Only staging follow-through if staging was open
            if stub.get("staging_open"):
                row = run_profile_at_start(
                    coin=coin,
                    start_index=start_index,
                    profile="two_early_medium",
                    candles=candles,
                    max_window_candles=max_win,
                    capture_economics=True,
                )
                stub.update(
                    {
                        "followthrough_status": "extended",
                        "followthrough_flat": int(row.get("trade_flat") or 0),
                        "followthrough_total_pnl": row.get("total_pnl"),
                        "followthrough_duration": row.get("duration_candles"),
                        "followthrough_closed_later": int(row.get("trade_flat") or 0) == 1,
                    }
                )
            else:
                stub["followthrough_status"] = "staging_already_flat"
        except Exception as exc:  # noqa: BLE001
            stub["followthrough_status"] = f"error:{exc}"
        out.append(stub)
    return out


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
    max_windows_per_coin: int | None,
    max_pairs: int | None,
    skip_followthrough: bool,
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
        coin_candles = {}
        for coin in manifest["coins"]:
            coin_candles[coin] = normalize_candles(
                coin,
                load_candles_for_symbol(coin, limit=manifest.get("candle_limit")),
            )
        log(f"[resume] {len(start_rows)} planned pairs from manifest")
    else:
        included, excluded, window_rows, start_rows, coin_candles, manifest = plan_universe_and_starts(
            candle_limit=candle_limit,
            target_per_window=target_per_window,
            seed=seed,
            warmup=warmup,
            smoke=smoke,
            smoke_coins=smoke_coins,
            max_windows_per_coin=max_windows_per_coin,
        )
        if max_pairs is not None:
            start_rows = start_rows[: int(max_pairs)]
            manifest["n_planned_pairs"] = len(start_rows)
            manifest["n_planned_profile_runs"] = len(start_rows) * 2
            manifest["max_pairs_applied"] = int(max_pairs)
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
                # Overlay windowed keys / metadata
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

    # Follow-through diagnostic
    start_meta = {str(r["pair_key"]): r for r in start_rows}
    # Build temporary pairs for follow-through selection
    by_key: dict[str, dict[str, dict[str, Any]]] = {}
    for row in raw_rows:
        by_key.setdefault(str(row["pair_key"]), {})[str(row["profile"])] = row
    tmp_pairs = []
    for pk, profs in by_key.items():
        if "legacy" in profs and "two_early_medium" in profs:
            tmp_pairs.append(
                compare_window_pair(profs["legacy"], profs["two_early_medium"], start_meta.get(pk, {"pair_key": pk}))
            )
    if skip_followthrough:
        followthrough_rows = open_mtm_followthrough_stub_rows(tmp_pairs)
        for r in followthrough_rows:
            r["followthrough_status"] = "skipped"
    else:
        followthrough_rows = run_followthrough(
            pairs=tmp_pairs,
            start_meta=start_meta,
            coin_candles=coin_candles,
            smoke=smoke,
        )

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
        followthrough_rows=followthrough_rows,
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


def print_manual_commands(output_dir: Path, log_dir: Path = DEFAULT_LOG_DIR) -> None:
    out = str(output_dir)
    logs = str(log_dir)
    print(
        "\n=== MANUAL FULL RUN (do not auto-start from agent) ===\n"
        f"mkdir -p {logs} && \\\n"
        f"nohup env PYTHONPATH=. python -m research.backtests.run_two_early_medium_large_multicoin_window_validation \\\n"
        f"  --output-dir {out} \\\n"
        f"  --target-per-window 25 \\\n"
        f"  --seed 20260721 \\\n"
        f"  > {logs}/tem_large_window_full_run.out 2>&1 &\n"
        f"echo $! > {logs}/tem_large_window_full_run.pid\n"
        "\n# Follow:\n"
        f"tail -f {logs}/tem_large_window_full_run.out\n"
        "\n# Process status:\n"
        f"ps -p $(cat {logs}/tem_large_window_full_run.pid) -o pid,etime,cmd\n"
        "\n# Resume (coins/windows reload from manifest):\n"
        f"nohup env PYTHONPATH=. python -m research.backtests.run_two_early_medium_large_multicoin_window_validation \\\n"
        f"  --output-dir {out} --resume \\\n"
        f"  > {logs}/tem_large_window_full_run_resume.out 2>&1 &\n"
        f"echo $! > {logs}/tem_large_window_full_run.pid\n"
    )


def estimate_full_size() -> dict[str, Any]:
    included, excluded = discover_coin_universe()
    n_coins = len(included)
    # Rough: ~4-5 windows × 25 starts
    est_pairs = n_coins * 4 * TARGET_STARTS_PER_WINDOW
    return {
        "n_coins_included": n_coins,
        "n_coins_excluded": len(excluded),
        "approx_windows_per_coin": "4–5 (early/middle/late[/recent]/full_history)",
        "target_starts_per_window": TARGET_STARTS_PER_WINDOW,
        "approx_planned_pairs": est_pairs,
        "approx_profile_runs": est_pairs * 2,
        "excluded_sample": excluded[:5],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--log-dir", type=Path, default=DEFAULT_LOG_DIR)
    parser.add_argument("--candle-limit", type=int, default=FULL_HISTORY_CANDLE_LIMIT)
    parser.add_argument("--target-per-window", type=int, default=TARGET_STARTS_PER_WINDOW)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--warmup", type=int, default=DEFAULT_WARMUP)
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument("--smoke-coins", default="APTUSDT,TRXUSDT,ATOMUSDT")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--max-windows-per-coin", type=int, default=None)
    parser.add_argument("--max-pairs", type=int, default=None)
    parser.add_argument("--skip-followthrough", action="store_true")
    parser.add_argument("--print-manual-commands-only", action="store_true")
    parser.add_argument("--estimate-only", action="store_true")
    args = parser.parse_args(argv)

    if args.print_manual_commands_only:
        print_manual_commands(args.output_dir, args.log_dir)
        return 0
    if args.estimate_only:
        print(json.dumps(estimate_full_size(), indent=2, default=str))
        print_manual_commands(args.output_dir, args.log_dir)
        return 0

    smoke_coins = [c.strip().upper() for c in str(args.smoke_coins).split(",") if c.strip()]
    run_validation(
        output_dir=args.output_dir,
        candle_limit=args.candle_limit,
        target_per_window=args.target_per_window,
        seed=args.seed,
        warmup=args.warmup,
        smoke=bool(args.smoke),
        resume=bool(args.resume),
        smoke_coins=smoke_coins if args.smoke else None,
        max_windows_per_coin=2 if args.smoke and args.max_windows_per_coin is None else args.max_windows_per_coin,
        max_pairs=args.max_pairs,
        skip_followthrough=bool(args.skip_followthrough) or bool(args.smoke),
    )
    print_manual_commands(args.output_dir, args.log_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Historical causal C4 follow-up bucket sampler (research-only).

Replays starts once (legacy economics path), extracts first_leg/full_trigger when
CYCLE_4 SHORT_REDUCE follow-up is built, assigns distance buckets, and writes a
selected_starts.csv compatible with run_adaptive_distance_staging_validation.
"""

from __future__ import annotations

import argparse
import json
import time
import traceback
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from research.backtests.adaptive_distance_staging import (
    REAL_DISTANCE_BUCKETS,
    compute_original_distance_pct,
    select_distance_bucket,
    theoretical_bucket_label,
)
from research.backtests.candle_loader import load_candles_for_symbol
from research.backtests.historical_backtest import normalize_candles, run_historical_backtest
from research.backtests.inventory_mtm_freeze import safe_float
from research.backtests.multicoin_blocker_price_staging import (
    DEFAULT_BASELINE,
    FILL_MODEL,
    FULL_HISTORY_CANDLE_LIMIT,
    LONG_FILL_DISTANCE_PCT,
    LONG_NOTIONAL,
    TARGET_PROFIT_USDT,
    TP_PROFIT_TARGET_PCT,
)
from research.backtests.multicoin_price_staging_grid import (
    assert_output_dir_safe,
    atomic_write_json,
    load_csv_rows,
    write_csv,
)
from research.backtests.recovery_reentry_policy import load_baseline_blockers
from research.backtests.two_early_medium_window_plan import window_pair_key

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUT = ROOT / "research/backtests/results/adaptive_distance_bucket_candidates_20260722"
STAGE_C = ROOT / "research/backtests/results/adaptive_distance_staging_stage_c_20260722"

PRIMARY_COINS = (
    "APTUSDT",
    "BTCUSDT",
    "ETHUSDT",
    "SOLUSDT",
    "AVAXUSDT",
    "ARBUSDT",
    "OPUSDT",
    "LINKUSDT",
    "DOGEUSDT",
    "ADAUSDT",
    "XRPUSDT",
    "DOTUSDT",
    "ATOMUSDT",
    "NEARUSDT",
    "SUIUSDT",
    "SEIUSDT",
    "TIAUSDT",
    "INJUSDT",
    "RENDERUSDT",
    "WLDUSDT",
    "AAVEUSDT",
    "UNIUSDT",
    "LTCUSDT",
    "BCHUSDT",
    "FILUSDT",
    "ETCUSDT",
    "TRXUSDT",
)


def log(msg: str) -> None:
    print(msg, flush=True)


def _window_id_for_index(start_index: int, n_candles: int) -> str:
    if n_candles <= 0:
        return "full"
    frac = start_index / max(n_candles - 1, 1)
    if frac < 0.25:
        return "early"
    if frac < 0.5:
        return "middle"
    if frac < 0.75:
        return "late"
    return "recent"


def extract_c4_followup_distance(result: Any) -> dict[str, Any] | None:
    """Causal C4 SHORT_REDUCE distance from intents or research plans."""
    excerpt = dict(getattr(result, "final_strategy_state_excerpt", None) or {})
    plans = list(excerpt.get("research_second_leg_price_staging_plans") or [])
    for row in plans:
        if int(row.get("cycle_index") or 0) != 4:
            continue
        first = safe_float(row.get("first_leg_fill_price"))
        full = safe_float(row.get("full_trigger_price"))
        if first > 0 and full > 0:
            d = compute_original_distance_pct(first, full)
            if d is None:
                continue
            bucket = theoretical_bucket_label(select_distance_bucket(d))
            return {
                "first_leg_fill_price": first,
                "full_trigger_price": full,
                "original_distance_pct": d,
                "distance_bucket": bucket,
                "source": "research_plan",
                "required_net": row.get("required_net"),
            }

    c4_intents = []
    for intent in getattr(result, "intent_log", None) or []:
        purpose = str(intent.get("purpose") or "")
        if "CYCLE_4_SHORT_REDUCE" not in purpose.upper():
            continue
        c4_intents.append(intent)
    if not c4_intents:
        return None
    triggers = []
    first = 0.0
    required_net = None
    for intent in c4_intents:
        meta = dict(intent.get("metadata_excerpt") or intent.get("metadata") or {})
        tp = safe_float(intent.get("trigger_price"))
        if tp > 0:
            triggers.append(tp)
        fl = safe_float(meta.get("first_leg_fill_price"))
        if fl > 0:
            first = fl
        rn = safe_float(meta.get("required_net") or meta.get("stage_required_net_total"))
        if rn > 0:
            required_net = rn
    if first <= 0 or not triggers:
        return None
    full = min(triggers)
    d = compute_original_distance_pct(first, full)
    if d is None:
        return None
    bucket = theoretical_bucket_label(select_distance_bucket(d))
    return {
        "first_leg_fill_price": first,
        "full_trigger_price": full,
        "original_distance_pct": d,
        "distance_bucket": bucket,
        "source": "intent_log",
        "required_net": required_net,
    }


def _run_probe(
    *,
    coin: str,
    start_index: int,
    candles: list[Any],
    max_window_candles: int,
) -> dict[str, Any] | None:
    from research.backtests.multicoin_blocker_price_staging import run_isolated_blocker
    from research.backtests.second_leg_price_staging import resolve_grid_profile

    end = min(len(candles), int(start_index) + int(max_window_candles))
    series = candles[:end]
    if int(start_index) >= len(series):
        return None
    cfg = resolve_grid_profile("two_early_medium")
    result = run_isolated_blocker(
        coin=coin,
        candles=series,
        start_index=int(start_index),
        staging_config=cfg,
    )
    return extract_c4_followup_distance(result)


def _candidate_starts_for_coin(
    *,
    coin: str,
    n_candles: int,
    warmup: int,
    grid_step: int,
    max_starts: int,
    blocker_starts: list[int],
    seed_starts: list[int],
) -> list[tuple[int, str]]:
    """Return (start_index, origin) candidates — blockers/seeds first, then grid."""
    out: list[tuple[int, str]] = []
    seen: set[int] = set()
    min_remaining = 800
    hi = max(warmup, n_candles - min_remaining)

    def add(idx: int, origin: str) -> None:
        if idx < warmup or idx > hi:
            return
        if idx in seen:
            return
        seen.add(idx)
        out.append((idx, origin))

    for s in seed_starts:
        add(int(s), "stage_c_seed")
    for s in blocker_starts:
        add(int(s), "historical_blocker")
    # Dense-ish grid
    step = max(200, int(grid_step))
    for idx in range(warmup, hi + 1, step):
        add(idx, "grid")
        if len(out) >= max_starts:
            break
    return out[:max_starts]


def _select_diverse(
    candidates: list[dict[str, Any]],
    *,
    target_per_bucket: int,
    max_coin_share: float = 0.30,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    selected: list[dict[str, Any]] = []
    exclusions: list[dict[str, Any]] = []
    by_bucket: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for c in candidates:
        b = c.get("distance_bucket")
        if b in REAL_DISTANCE_BUCKETS:
            by_bucket[str(b)].append(c)

    for bucket in REAL_DISTANCE_BUCKETS:
        pool = list(by_bucket.get(bucket, []))
        # Prefer diversity: rotate coins
        chosen: list[dict[str, Any]] = []
        coin_counts: Counter[str] = Counter()
        used_keys: set[str] = set()
        # Sort: prefer blockers mixed with others, then by distance uniqueness
        pool.sort(
            key=lambda r: (
                0 if r.get("origin") == "historical_blocker" else 1,
                r.get("coin") or "",
                float(r.get("original_distance_pct") or 0),
            )
        )
        changed = True
        while changed and len(chosen) < target_per_bucket:
            changed = False
            for row in pool:
                if len(chosen) >= target_per_bucket:
                    break
                pk = str(row["pair_key"])
                if pk in used_keys:
                    continue
                coin = str(row["coin"])
                limit = max(1, int(target_per_bucket * max_coin_share))
                # Relax cap only if pool is thin
                if coin_counts[coin] >= limit and len(pool) >= target_per_bucket:
                    continue
                chosen.append(row)
                used_keys.add(pk)
                coin_counts[coin] += 1
                changed = True
        # Fill remaining without coin cap if still short
        if len(chosen) < target_per_bucket:
            for row in pool:
                if len(chosen) >= target_per_bucket:
                    break
                pk = str(row["pair_key"])
                if pk in used_keys:
                    continue
                chosen.append(row)
                used_keys.add(pk)
        selected.extend(chosen)
        for row in pool:
            if str(row["pair_key"]) not in used_keys:
                exclusions.append({**row, "exclude_reason": f"bucket_{bucket}_quota_full_or_dup"})

    # Mark scarcity
    for bucket in REAL_DISTANCE_BUCKETS:
        n = sum(1 for r in selected if r.get("distance_bucket") == bucket)
        if n < target_per_bucket:
            for r in selected:
                if r.get("distance_bucket") == bucket:
                    r["sample_note"] = "sample_insufficient" if n < 10 else "below_target_25"

    return selected, exclusions


def _to_start_row(c: dict[str, Any]) -> dict[str, Any]:
    return {
        "coin": c["coin"],
        "window_id": c["window_id"],
        "window_kind": c.get("window_kind") or c["window_id"],
        "start_index": c["start_index"],
        "run_end_index": c.get("run_end_index"),
        "max_window_candles": c["max_window_candles"],
        "pair_key": c["pair_key"],
        "primary_category": c.get("primary_category")
        or ("historical_blocker" if c.get("origin") == "historical_blocker" else "bucket_sampler"),
        "categories": c.get("categories") or c.get("origin"),
        "selection_rank": c.get("selection_rank"),
        "is_historical_blocker": int(c.get("origin") == "historical_blocker"),
        "is_neutral_pool": int(c.get("origin") == "grid"),
        "original_distance_pct": c.get("original_distance_pct"),
        "distance_bucket": c.get("distance_bucket"),
        "first_leg_fill_price": c.get("first_leg_fill_price"),
        "full_trigger_price": c.get("full_trigger_price"),
        "sampler_origin": c.get("origin"),
        "sample_note": c.get("sample_note"),
    }


def run_sampler(
    *,
    output_dir: Path,
    coins: list[str],
    target_per_bucket: int,
    grid_step: int,
    max_starts_per_coin: int,
    max_window_candles: int,
    warmup: int,
    candle_limit: int | None,
    seed_from_stage_c: bool,
    max_seconds: float | None,
) -> dict[str, Any]:
    assert_output_dir_safe(output_dir, resume=False)
    output_dir.mkdir(parents=True, exist_ok=True)
    t0 = time.time()

    blocker_by_coin: dict[str, list[int]] = defaultdict(list)
    blocker_path = DEFAULT_BASELINE / "blocker_trades.csv"
    if blocker_path.exists():
        for b in load_baseline_blockers(blocker_path):
            coin = str(b.get("coin") or b.get("symbol") or "").upper()
            idx = b.get("start_index") or b.get("absolute_start_index") or b.get("trade_start_index")
            if coin and idx is not None:
                blocker_by_coin[coin].append(int(idx))

    stage_c_seeds: dict[str, list[int]] = defaultdict(list)
    stage_c_direct: list[dict[str, Any]] = []
    if seed_from_stage_c and (STAGE_C / "raw_profile_runs.csv").exists():
        starts_meta = {
            str(r["pair_key"]): r
            for r in load_csv_rows(STAGE_C / "start_points.csv")
        } if (STAGE_C / "start_points.csv").exists() else {}
        for r in load_csv_rows(STAGE_C / "raw_profile_runs.csv"):
            if r.get("profile") != "adaptive_equal":
                continue
            if not r.get("original_distance_pct"):
                continue
            coin = str(r["coin"]).upper()
            start_index = int(float(r["start_index"]))
            stage_c_seeds[coin].append(start_index)
            d = float(r["original_distance_pct"])
            bucket = r.get("distance_bucket") or theoretical_bucket_label(select_distance_bucket(d))
            meta = starts_meta.get(str(r.get("pair_key")), {})
            stage_c_direct.append(
                {
                    "coin": coin,
                    "window_id": r.get("window_id") or meta.get("window_id") or "stage_c",
                    "window_kind": meta.get("window_kind") or r.get("window_id") or "stage_c",
                    "start_index": start_index,
                    "run_end_index": meta.get("run_end_index"),
                    "max_window_candles": int(
                        float(meta.get("max_window_candles") or max_window_candles)
                    ),
                    "pair_key": r.get("pair_key")
                    or window_pair_key(coin, str(r.get("window_id") or "stage_c"), start_index),
                    "origin": "stage_c_seed",
                    "primary_category": meta.get("primary_category") or "stage_c_seed",
                    "categories": meta.get("categories"),
                    "is_historical_blocker": meta.get("is_historical_blocker"),
                    "original_distance_pct": d,
                    "distance_bucket": bucket,
                    "first_leg_fill_price": r.get("first_leg_fill_price"),
                    "full_trigger_price": r.get("full_trigger_price"),
                    "source": "stage_c_export",
                }
            )

    all_candidates: list[dict[str, Any]] = list(stage_c_direct)
    exclusions: list[dict[str, Any]] = []
    scanned = 0
    errors = 0
    bucket_pool: Counter[str] = Counter(
        str(c["distance_bucket"]) for c in all_candidates if c.get("distance_bucket")
    )
    if stage_c_direct:
        log(f"[seed] imported {len(stage_c_direct)} Stage-C C4 distance cases")

    for coin in coins:
        if max_seconds is not None and (time.time() - t0) > max_seconds:
            log(f"[time-budget] stop before {coin}")
            break
        if all(bucket_pool[b] >= target_per_bucket for b in REAL_DISTANCE_BUCKETS):
            log("[coverage] all buckets reached target — stopping scan")
            break
        try:
            candles = normalize_candles(coin, load_candles_for_symbol(coin, limit=candle_limit))
        except Exception as exc:  # noqa: BLE001
            exclusions.append({"coin": coin, "exclude_reason": f"load_failed:{exc}"})
            continue
        if len(candles) < warmup + 1000:
            exclusions.append({"coin": coin, "exclude_reason": "insufficient_candles"})
            continue
        starts = _candidate_starts_for_coin(
            coin=coin,
            n_candles=len(candles),
            warmup=warmup,
            grid_step=grid_step,
            max_starts=max_starts_per_coin,
            blocker_starts=blocker_by_coin.get(coin, []),
            seed_starts=stage_c_seeds.get(coin, []),
        )
        log(f"[{coin}] probing {len(starts)} starts (n_candles={len(candles)})")
        known_keys = {str(c["pair_key"]) for c in all_candidates}
        for start_index, origin in starts:
            if max_seconds is not None and (time.time() - t0) > max_seconds:
                break
            # Keep scanning while any bucket is below target; do not require 2× on scarce buckets.
            if all(bucket_pool[b] >= target_per_bucket for b in REAL_DISTANCE_BUCKETS):
                break
            window_id = _window_id_for_index(start_index, len(candles))
            pair_key = window_pair_key(coin, window_id, start_index)
            if pair_key in known_keys:
                continue
            # Prefer probing while small buckets are scarce (still probe others for diversity).
            scarce = [b for b in ("0_2", "2_4") if bucket_pool[b] < target_per_bucket]
            if scarce and origin == "grid" and bucket_pool["4_7"] >= target_per_bucket and bucket_pool["gt_7"] >= target_per_bucket:
                # Still probe grids — small distances are rare; no skip.
                pass
            scanned += 1
            try:
                hit = _run_probe(
                    coin=coin,
                    start_index=start_index,
                    candles=candles,
                    max_window_candles=max_window_candles,
                )
            except Exception as exc:  # noqa: BLE001
                errors += 1
                exclusions.append(
                    {
                        "pair_key": pair_key,
                        "coin": coin,
                        "start_index": start_index,
                        "exclude_reason": f"probe_error:{exc}",
                        "traceback": traceback.format_exc()[-500:],
                    }
                )
                continue
            if hit is None or not hit.get("distance_bucket"):
                exclusions.append(
                    {
                        "pair_key": pair_key,
                        "coin": coin,
                        "start_index": start_index,
                        "origin": origin,
                        "exclude_reason": "no_c4_followup",
                    }
                )
                continue
            row = {
                "coin": coin,
                "window_id": window_id,
                "window_kind": window_id,
                "start_index": start_index,
                "run_end_index": min(len(candles), start_index + max_window_candles),
                "max_window_candles": max_window_candles,
                "pair_key": pair_key,
                "origin": origin,
                "primary_category": (
                    "historical_blocker" if origin == "historical_blocker" else origin
                ),
                **hit,
            }
            all_candidates.append(row)
            known_keys.add(pair_key)
            bucket_pool[str(hit["distance_bucket"])] += 1
            log(
                f"  hit {pair_key} d={hit['original_distance_pct']:.3f}% "
                f"bucket={hit['distance_bucket']} origin={origin}"
            )

    selected, sel_excl = _select_diverse(all_candidates, target_per_bucket=target_per_bucket)
    exclusions.extend(sel_excl)
    start_rows = [_to_start_row(c) for c in selected]

    write_csv(output_dir / "all_candidates.csv", all_candidates)
    write_csv(output_dir / "selected_starts.csv", start_rows)
    write_csv(output_dir / "exclusions.csv", exclusions)

    bucket_counts = [
        {
            "distance_bucket": b,
            "n_candidates": sum(1 for c in all_candidates if c.get("distance_bucket") == b),
            "n_selected": sum(1 for c in selected if c.get("distance_bucket") == b),
            "target": target_per_bucket,
            "sample_sufficient": int(
                sum(1 for c in selected if c.get("distance_bucket") == b) >= 10
            ),
        }
        for b in REAL_DISTANCE_BUCKETS
    ]
    write_csv(output_dir / "bucket_counts.csv", bucket_counts)

    coin_dist = []
    for b in REAL_DISTANCE_BUCKETS:
        ctr = Counter(c["coin"] for c in selected if c.get("distance_bucket") == b)
        for coin, n in sorted(ctr.items()):
            coin_dist.append({"distance_bucket": b, "coin": coin, "n": n})
    write_csv(output_dir / "bucket_coin_distribution.csv", coin_dist)

    win_dist = []
    for b in REAL_DISTANCE_BUCKETS:
        ctr = Counter(c["window_id"] for c in selected if c.get("distance_bucket") == b)
        for w, n in sorted(ctr.items()):
            win_dist.append({"distance_bucket": b, "window_id": w, "n": n})
    write_csv(output_dir / "bucket_window_distribution.csv", win_dist)

    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "coins": coins,
        "target_per_bucket": target_per_bucket,
        "grid_step": grid_step,
        "max_starts_per_coin": max_starts_per_coin,
        "max_window_candles": max_window_candles,
        "warmup": warmup,
        "scanned_starts": scanned,
        "n_candidates": len(all_candidates),
        "n_selected": len(selected),
        "errors": errors,
        "elapsed_sec": round(time.time() - t0, 2),
        "bucket_pool": dict(bucket_pool),
    }
    integrity = {
        "generated_at": manifest["generated_at"],
        "pass": True,
        "bucket_counts": bucket_counts,
        "sample_insufficient_buckets": [
            r["distance_bucket"] for r in bucket_counts if not r["sample_sufficient"]
        ],
        "n_selected": len(selected),
        "scanned_starts": scanned,
        "note": "Relative research sampler — no PnL selection; economics unchanged.",
    }
    atomic_write_json(output_dir / "run_manifest.json", manifest)
    atomic_write_json(output_dir / "integrity.json", integrity)

    report = [
        "# Adaptive Distance Bucket Sampler",
        "",
        f"Generated: `{manifest['generated_at']}`",
        f"Scanned starts: **{scanned}**",
        f"Candidates: **{len(all_candidates)}**",
        f"Selected: **{len(selected)}**",
        f"Elapsed: {manifest['elapsed_sec']}s",
        "",
        "## Bucket counts",
        "",
    ]
    for r in bucket_counts:
        report.append(
            f"- `{r['distance_bucket']}`: candidates={r['n_candidates']}, "
            f"selected={r['n_selected']}/{r['target']}, "
            f"sufficient={bool(r['sample_sufficient'])}"
        )
    report.extend(
        [
            "",
            "## Notes",
            "",
            "- No economics changes; distance from causal C4 follow-up intents.",
            "- selected_starts.csv is ready for `--mode bucket_coverage --starts ...`.",
            "",
        ]
    )
    (output_dir / "REPORT.md").write_text("\n".join(report) + "\n", encoding="utf-8")
    log(json.dumps(integrity, indent=2))
    return integrity


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--out", type=Path, default=DEFAULT_OUT)
    p.add_argument("--coins", default=",".join(PRIMARY_COINS))
    p.add_argument("--target-per-bucket", type=int, default=25)
    p.add_argument("--grid-step", type=int, default=2500)
    p.add_argument("--max-starts-per-coin", type=int, default=40)
    p.add_argument("--max-window-candles", type=int, default=12_000)
    p.add_argument("--warmup", type=int, default=500)
    p.add_argument("--candle-limit", type=int, default=FULL_HISTORY_CANDLE_LIMIT)
    p.add_argument("--seed-from-stage-c", action="store_true", default=True)
    p.add_argument("--no-seed-from-stage-c", action="store_true")
    p.add_argument("--max-seconds", type=float, default=None)
    args = p.parse_args(argv)
    coins = [c.strip().upper() for c in str(args.coins).split(",") if c.strip()]
    run_sampler(
        output_dir=args.out,
        coins=coins,
        target_per_bucket=args.target_per_bucket,
        grid_step=args.grid_step,
        max_starts_per_coin=args.max_starts_per_coin,
        max_window_candles=args.max_window_candles,
        warmup=args.warmup,
        candle_limit=args.candle_limit,
        seed_from_stage_c=bool(args.seed_from_stage_c) and not bool(args.no_seed_from_stage_c),
        max_seconds=args.max_seconds,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

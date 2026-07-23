#!/usr/bin/env python3
"""Continuous chronological validation: legacy vs TEM vs adaptive_equal @1000/500.

True continuous semantics (one trade at a time per coin×profile). No multi-start,
no FULL_DYNAMIC, no fixed-step, no live integration, no overwrite of prior outputs.
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
import subprocess
import time
import traceback
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from research.backtests.candle_loader import load_candles_for_symbol
from research.backtests.historical_backtest import normalize_candles
from research.backtests.inventory_mtm_freeze import safe_float
from research.backtests.multicoin_blocker_price_staging import (
    FULL_HISTORY_CANDLE_LIMIT,
    LONG_NOTIONAL,
    SHORT_NOTIONAL,
)
from research.backtests.multicoin_price_staging_grid import (
    atomic_write_json,
    load_checkpoint,
    load_csv_rows,
    write_csv,
)
from research.backtests.run_inventory_mtm_neg1_policy_audit import (
    FILL_MODEL,
    LONG_FILL_DISTANCE_PCT,
    TARGET_PROFIT_USDT,
    TP_PROFIT_TARGET_PCT,
)
from research.backtests.staging_profiles_continuous import (
    ALLOWED_PROFILES,
    check_overlap_integrity,
    classify_blocker_root_cause,
    first_trade_parity_rows,
    leave_one_coin_out,
    run_continuous_sequence,
    safety_aggregate,
    summarize_by_profile,
    summarize_coin_profile,
    validate_profiles,
)
from research.backtests.two_early_medium_multistart_starts import DEFAULT_WARMUP

ROOT = Path(__file__).resolve().parents[2]
SOURCE_LARGE = (
    ROOT
    / "research/backtests/results/fixed_step_distance_staging_large_1000_500_20260722"
)
DEFAULT_OUT = (
    ROOT
    / "research/backtests/results/staging_profiles_continuous_1000_500_20260722"
)
DEFAULT_LOG_DIR = ROOT / "research/backtests/results/_logs"
PROTECTED = (
    SOURCE_LARGE,
    ROOT / "research/backtests/results/tem_full_dynamic_blocker_validation_20260722",
    ROOT / "research/backtests/results/tem_fd_undercoverage_fix_20260722",
    ROOT / "research/backtests/results/tem_fd_pnl_source_audit_20260722",
    ROOT / "research/backtests/results/multicoin_price_staging_grid_1000_500_20260721",
    ROOT / "research/backtests/results/two_early_medium_candidate_validation_1000_500_20260721",
)

RAW_FIELDS = [
    "coin",
    "profile",
    "trade_id",
    "trade_number",
    "start_bar",
    "end_bar",
    "flat_bar",
    "next_start_bar",
    "trade_flat",
    "is_blocker",
    "status",
    "exit_reason",
    "realized_pnl",
    "open_mtm",
    "total_pnl",
    "closed_pnl",
    "duration_candles",
    "max_cycle",
    "coverage_class",
    "economic_undercoverage_closed",
    "sufficient_false_closed",
    "invalid_partial",
    "over_close",
    "duplicate_stage",
    "orphan_stage_order",
    "late_stage_fill_after_exit",
    "filled_stages",
    "planned_stages",
    "cancelled_stage_indices",
    "staging_activated",
    "gross_exposure",
    "net_exposure",
    "max_drawdown_pct",
    "final_long_qty",
    "final_short_qty",
    "final_price",
    "pending_cycle_loss_usdt",
    "distance_to_exit_pct",
    "recovery_active",
    "refill_active",
    "blocker_root_cause",
    "pnl_reconcile_ok",
    "first_timestamp",
    "last_timestamp",
]


def log(msg: str) -> None:
    print(msg, flush=True)


def _git_status() -> dict[str, Any]:
    status: dict[str, Any] = {"commit": None, "dirty": None}
    try:
        status["commit"] = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip()
        porcelain = subprocess.check_output(
            ["git", "status", "--porcelain"], cwd=ROOT, text=True
        )
        status["dirty"] = bool(porcelain.strip())
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        status["error"] = str(exc)
    return status


def assert_output_dir_safe(output_dir: Path, *, resume: bool) -> None:
    resolved = output_dir.resolve()
    for protected in PROTECTED:
        if resolved == protected.resolve():
            raise RuntimeError(f"refusing protected output dir: {protected}")
    if resume:
        return
    if output_dir.exists() and any(output_dir.iterdir()):
        raise FileExistsError(
            f"refusing to overwrite non-empty output dir: {output_dir} "
            "(use --resume or a new path)"
        )


def _ts_of(candle: Any) -> str:
    raw = candle.get("timestamp") if isinstance(candle, dict) else getattr(candle, "timestamp", None)
    if raw is None:
        return ""
    if hasattr(raw, "isoformat"):
        return raw.isoformat()
    return str(raw)


def load_source_universe(source_dir: Path) -> tuple[list[str], list[dict[str, Any]], dict[str, Any]]:
    universe_path = source_dir / "coin_universe.csv"
    excluded_path = source_dir / "excluded_coins.csv"
    manifest_path = source_dir / "run_manifest.json"
    if not universe_path.exists():
        raise FileNotFoundError(f"missing source universe: {universe_path}")
    coins = [
        str(r["coin"]).upper()
        for r in csv.DictReader(universe_path.open(encoding="utf-8"))
        if str(r.get("included") or "True").lower() in {"1", "true", "yes"}
    ]
    excluded = list(csv.DictReader(excluded_path.open(encoding="utf-8"))) if excluded_path.exists() else []
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
    return coins, excluded, manifest


def inherited_config_payload(source_manifest: dict[str, Any]) -> dict[str, Any]:
    return {
        "source_run": str(SOURCE_LARGE.relative_to(ROOT)),
        "profiles_this_run": list(ALLOWED_PROFILES),
        "sizes": source_manifest.get("sizes") or f"{int(LONG_NOTIONAL)}:{int(SHORT_NOTIONAL)}",
        "initial_long_usdt": LONG_NOTIONAL,
        "initial_short_usdt": SHORT_NOTIONAL,
        "hedge_ratio_short": 0.5,
        "warmup": int(source_manifest.get("warmup") or DEFAULT_WARMUP),
        "candle_limit": int(source_manifest.get("candle_limit") or FULL_HISTORY_CANDLE_LIMIT),
        "fill_model": FILL_MODEL,
        "config_source": "live",
        "direction": "long",
        "tp_profit_target_pct": TP_PROFIT_TARGET_PCT,
        "long_fill_distance_pct": LONG_FILL_DISTANCE_PCT,
        "target_profit_usdt": TARGET_PROFIT_USDT,
        "intrabar_causality": "conservative_fill_model",
        "coverage_gates": "FinalExitEconomics / C4 basket coverage (unchanged)",
        "data_end_mtm": "open_mtm = unrealized_pnl; total_pnl = realized_pnl + open_mtm",
        "continuous_semantics": {
            "multi_start": False,
            "overlapping_trades": False,
            "next_start_rule": "next_start_bar = flat_end_bar + 1",
            "stop_on_open_at_data_end": True,
        },
        "excluded_from_this_run": [
            "fixed_step_1pct_equal",
            "fixed_step_2pct_equal",
            "adaptive_backloaded",
            "two_early_medium_full_dynamic",
            "FULL_DYNAMIC",
        ],
        "note": (
            "Per-coin independent capital; NOT a shared-wallet portfolio. "
            "Exposure concurrency is diagnostic only."
        ),
    }


def _empty_checkpoint(*, coins: list[str], profiles: Sequence[str]) -> dict[str, Any]:
    return {
        "version": 1,
        "coins": list(coins),
        "profiles": list(profiles),
        "completed_keys": [],
        "errors": [],
        "updated_at": None,
    }


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


def enrich_trade_row(trade: dict[str, Any], candles: list[Any]) -> dict[str, Any]:
    start = int(trade.get("start_bar") or trade.get("start_index") or 0)
    end = int(trade.get("end_bar") or start)
    first_ts = _ts_of(candles[start]) if 0 <= start < len(candles) else ""
    last_i = min(max(end - 1, start), len(candles) - 1) if candles else 0
    last_ts = _ts_of(candles[last_i]) if candles else ""
    out = dict(trade)
    out["blocker_root_cause"] = classify_blocker_root_cause(trade)
    out["first_timestamp"] = first_ts
    out["last_timestamp"] = last_ts
    # Normalize cancelled stages for CSV
    cancelled = out.get("cancelled_stage_indices")
    if isinstance(cancelled, (list, tuple)):
        out["cancelled_stage_indices"] = list(cancelled)
    return out


def build_common_window(
    coin_meta: dict[str, dict[str, Any]],
) -> tuple[str | None, str | None, list[str]]:
    starts = []
    ends = []
    for coin, meta in coin_meta.items():
        ft = meta.get("first_timestamp")
        lt = meta.get("last_timestamp")
        if ft and lt:
            starts.append((str(ft), coin))
            ends.append((str(lt), coin))
    if not starts or not ends:
        return None, None, []
    common_start = max(s for s, _ in starts)
    common_end = min(e for e, _ in ends)
    covered = [
        c
        for c, meta in coin_meta.items()
        if str(meta.get("first_timestamp") or "") <= common_start
        and str(meta.get("last_timestamp") or "") >= common_end
    ]
    return common_start, common_end, sorted(covered)


def filter_trades_common_window(
    trades: list[dict[str, Any]],
    *,
    common_start: str,
    common_end: str,
    coins: set[str],
) -> list[dict[str, Any]]:
    out = []
    for t in trades:
        if str(t.get("coin") or "").upper() not in coins:
            continue
        ft = str(t.get("first_timestamp") or "")
        lt = str(t.get("last_timestamp") or "")
        if not ft or not lt:
            continue
        # Keep trades whose start is inside the common window.
        if ft < common_start or ft > common_end:
            continue
        out.append(t)
    return out


def exposure_concurrency_rows(trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Diagnostic: simultaneous open trades across coins (not portfolio margin)."""
    events: list[tuple[str, int, str, str]] = []
    for t in trades:
        coin = str(t.get("coin") or "")
        profile = str(t.get("profile") or "")
        start = str(t.get("first_timestamp") or "")
        end = str(t.get("last_timestamp") or "")
        if not start or not end:
            continue
        events.append((start, 1, coin, profile))
        events.append((end, -1, coin, profile))
    by_profile: dict[str, list[tuple[str, int, str]]] = defaultdict(list)
    for ts, delta, coin, profile in events:
        by_profile[profile].append((ts, delta, coin))

    rows: list[dict[str, Any]] = []
    for profile, evs in sorted(by_profile.items()):
        evs.sort(key=lambda x: (x[0], -x[1]))
        open_n = 0
        max_open = 0
        max_ts = ""
        blockers_open = 0
        max_blockers = 0
        # Approximate initial capital bound: open_n * (1000+500)
        for ts, delta, _coin in evs:
            open_n += delta
            if open_n > max_open:
                max_open = open_n
                max_ts = ts
            max_blockers = max(max_blockers, blockers_open)
        # blocker peaks need trade-level scan
        open_intervals = [
            t
            for t in trades
            if str(t.get("profile")) == profile and int(t.get("is_blocker") or 0) == 1
        ]
        # crude: count blockers (open at end) as concurrent if timestamps overlap loosely
        max_blockers = len(open_intervals)
        rows.append(
            {
                "profile": profile,
                "max_concurrent_open_trades": max_open,
                "max_concurrent_ts": max_ts,
                "theoretical_max_initial_capital_usdt": max_open
                * (LONG_NOTIONAL + SHORT_NOTIONAL),
                "blocker_open_at_data_end": max_blockers,
                "note": "diagnostic only; no shared wallet / liquidation model",
            }
        )
    return rows


def monthly_summary(trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for t in trades:
        ft = str(t.get("first_timestamp") or "")
        month = ft[:7] if len(ft) >= 7 else "unknown"
        buckets[(str(t.get("profile")), month)].append(t)
    rows = []
    for (profile, month), group in sorted(buckets.items()):
        realized = sum(safe_float(t.get("realized_pnl")) for t in group)
        open_mtm = sum(safe_float(t.get("open_mtm")) for t in group)
        rows.append(
            {
                "profile": profile,
                "month": month,
                "trades_started": len(group),
                "flat_closed": sum(1 for t in group if int(t.get("trade_flat") or 0) == 1),
                "blockers": sum(1 for t in group if int(t.get("is_blocker") or 0) == 1),
                "sum_realized_pnl": realized,
                "sum_open_mtm": open_mtm,
                "total_pnl": realized + open_mtm,
            }
        )
    return rows


def decide_preliminary(
    *,
    summary_profile: list[dict[str, Any]],
    leaveouts: list[dict[str, Any]],
    safety: dict[str, Any],
    common_summary: list[dict[str, Any]],
) -> dict[str, Any]:
    by_p = {str(r["profile"]): r for r in summary_profile}
    names = [p for p in ALLOWED_PROFILES if p in by_p]
    ranked = sorted(names, key=lambda p: safe_float(by_p[p]["total_pnl"]), reverse=True)

    def _without_best(profile: str) -> float | None:
        rows = [
            r
            for r in leaveouts
            if r["profile"] == profile and str(r["left_out_coin"]).startswith("BEST:")
        ]
        return safe_float(rows[0]["total_pnl_without_coin"]) if rows else None

    common_by = {str(r["profile"]): r for r in common_summary}
    questions = {
        "q1_adaptive_equal_better_total_than_tem": None,
        "q2_tem_better_closed_realized": None,
        "q3_fewest_blockers": None,
        "q4_best_total_pnl_after_end_mtm": ranked[0] if ranked else None,
        "q5_advantage_without_best_coin": None,
        "q6_legacy_competitive_in_continuous": None,
        "q7_forward_test_candidate": None,
    }
    if "adaptive_equal" in by_p and "two_early_medium" in by_p:
        questions["q1_adaptive_equal_better_total_than_tem"] = bool(
            safe_float(by_p["adaptive_equal"]["total_pnl"])
            > safe_float(by_p["two_early_medium"]["total_pnl"])
        )
        questions["q2_tem_better_closed_realized"] = bool(
            safe_float(by_p["two_early_medium"]["sum_realized_pnl"])
            > safe_float(by_p["adaptive_equal"]["sum_realized_pnl"])
        )
    if names:
        questions["q3_fewest_blockers"] = min(
            names, key=lambda p: int(by_p[p]["blocker_count"])
        )
    wbest = {p: _without_best(p) for p in names}
    if all(v is not None for v in wbest.values()) and ranked:
        questions["q5_advantage_without_best_coin"] = max(
            names, key=lambda p: float(wbest[p] or 0.0)
        ) == ranked[0]
    if "legacy" in by_p and ranked:
        leg = safe_float(by_p["legacy"]["total_pnl"])
        best = safe_float(by_p[ranked[0]]["total_pnl"])
        questions["q6_legacy_competitive_in_continuous"] = bool(
            abs(best - leg) < abs(best) * 0.25 or leg >= best * 0.85
        )

    safety_ok = int(safety.get("all_green") or 0) == 1
    winner = None
    rationale: list[str] = []
    if not safety_ok:
        rationale.append("Safety not fully green — no winner.")
    elif len(ranked) >= 2:
        top, second = ranked[0], ranked[1]
        top_row, sec_row = by_p[top], by_p[second]
        edge = safe_float(top_row["total_pnl"]) - safe_float(sec_row["total_pnl"])
        blockers_ok = int(top_row["blocker_count"]) <= int(sec_row["blocker_count"])
        realized_ok = safe_float(top_row["sum_realized_pnl"]) >= safe_float(
            sec_row["sum_realized_pnl"]
        ) - abs(safe_float(sec_row["sum_realized_pnl"])) * 0.5
        common_ok = True
        if top in common_by and second in common_by:
            common_ok = safe_float(common_by[top]["total_pnl"]) >= safe_float(
                common_by[second]["total_pnl"]
            )
        wbest_ok = True
        if wbest.get(top) is not None and wbest.get(second) is not None:
            wbest_ok = float(wbest[top] or 0) >= float(wbest[second] or 0)
        if edge > 0 and blockers_ok and realized_ok and common_ok and wbest_ok:
            winner = top
            rationale.append(
                f"{top} leads on total_pnl (+{edge:.2f}), blockers not worse, "
                "common-window and leave-best consistent."
            )
        else:
            rationale.append(
                "No robust winner: total_pnl lead not confirmed across blockers/"
                "realized/common-window/leave-best."
            )
    questions["q7_forward_test_candidate"] = winner or (
        questions["q3_fewest_blockers"] if safety_ok else None
    )

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "safety_all_green": safety_ok,
        "ranking_by_total_pnl": ranked,
        "profile_totals": {p: by_p[p] for p in ranked},
        "without_best_coin_total_pnl": wbest,
        "questions": questions,
        "winner": winner,
        "robust_winner": winner is not None,
        "rationale": rationale,
        "capital_model": "per_coin_independent_not_shared_wallet",
        "no_live_recommendation": True,
        "no_commit": True,
    }


def write_report(
    path: Path,
    *,
    manifest: dict[str, Any],
    decision: dict[str, Any],
    summary_profile: list[dict[str, Any]],
    safety: dict[str, Any],
    first_parity: list[dict[str, Any]],
) -> None:
    lines = [
        "# Staging Profiles Continuous Validation (1000/500)",
        "",
        f"Generated: `{manifest.get('generated_at')}`",
        "",
        "## 1. Continuous semantics",
        "",
        "- One chronological pass per coin×profile after warmup.",
        "- Next trade only after prior flat; `next_start_bar = flat_end_bar + 1`.",
        "- No multi-start windows, no overlapping trades, no FULL_DYNAMIC, no fixed-step.",
        "",
        "## 2. Overlap integrity",
        "",
        f"- Integrity fail rows: `{safety.get('integrity_fail_rows')}`",
        f"- overlap_detected: `{safety.get('overlap_detected')}`",
        f"- stale_orders_detected: `{safety.get('stale_orders_detected')}`",
        f"- active_position_at_next_start: `{safety.get('active_position_at_next_start')}`",
        "",
        "## 3. Data / coins",
        "",
        f"- Source: `{manifest.get('source_run')}`",
        f"- Coins: `{manifest.get('coins')}`",
        f"- Warmup: `{manifest.get('inherited', {}).get('warmup')}`",
        f"- Candle limit: `{manifest.get('inherited', {}).get('candle_limit')}`",
        f"- Sizes: `{manifest.get('inherited', {}).get('sizes')}`",
        "",
        "## 4. Profiles",
        "",
        f"- `{manifest.get('profiles')}`",
        "",
        "## 5–13. Profile summary",
        "",
    ]
    for row in summary_profile:
        lines.extend(
            [
                f"### {row['profile']}",
                "",
                f"- trades_started: `{row.get('trades_started')}`",
                f"- flat / open_end: `{row.get('trades_flat_closed')}` / `{row.get('trades_open_at_data_end')}`",
                f"- realized / open_mtm / total: "
                f"`{row.get('sum_realized_pnl'):.4f}` / `{row.get('sum_open_mtm'):.4f}` / "
                f"`{row.get('total_pnl'):.4f}`",
                f"- blockers: `{row.get('blocker_count')}` (rate `{row.get('blocker_rate')}`)",
                f"- equal-coin mean/median total: "
                f"`{row.get('equal_coin_mean_total_pnl')}` / `{row.get('equal_coin_median_total_pnl')}`",
                f"- stage_fills / orders / cancels: "
                f"`{row.get('stage_fills')}` / `{row.get('orders')}` / `{row.get('cancels')}`",
                "",
            ]
        )
    lines.extend(
        [
            "## Safety",
            "",
            f"```json\n{json.dumps(safety, indent=2)}\n```",
            "",
            "## First-trade parity",
            "",
            f"```json\n{json.dumps(first_parity, indent=2)}\n```",
            "",
            "## Decision",
            "",
            f"- ranking_by_total_pnl: `{decision.get('ranking_by_total_pnl')}`",
            f"- robust_winner: `{decision.get('winner')}`",
            f"- questions: `{json.dumps(decision.get('questions'), indent=2)}`",
            "",
            "## Notes",
            "",
            "- Capital model: per-coin independent (not shared wallet).",
            "- No commit. No live recommendation.",
            "",
        ]
    )
    for r in decision.get("rationale") or []:
        lines.append(f"- {r}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def rebuild_artifacts(
    *,
    output_dir: Path,
    manifest: dict[str, Any],
    coin_meta: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    trades = load_csv_rows(output_dir / "continuous_trades.csv")
    integrity = load_csv_rows(output_dir / "continuous_overlap_integrity.csv")
    # Recompute summaries from trades
    by_cp: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for t in trades:
        by_cp[(str(t["coin"]).upper(), str(t["profile"]))].append(t)

    coin_rows: list[dict[str, Any]] = []
    for (coin, profile), group in sorted(by_cp.items()):
        meta = coin_meta.get(coin) or {}
        coin_rows.append(
            summarize_coin_profile(
                group,
                coin=coin,
                profile=profile,
                n_candles=int(meta.get("n_candles") or 0),
                first_ts=meta.get("first_timestamp"),
                last_ts=meta.get("last_timestamp"),
            )
        )

    summary_profile = summarize_by_profile(coin_rows)
    # summary_by_coin: wide-ish per coin across profiles
    by_coin: dict[str, dict[str, Any]] = {}
    for r in coin_rows:
        coin = r["coin"]
        slot = by_coin.setdefault(coin, {"coin": coin})
        p = r["profile"]
        slot[f"{p}_total_pnl"] = r["total_pnl"]
        slot[f"{p}_realized"] = r["sum_realized_pnl"]
        slot[f"{p}_open_mtm"] = r["sum_open_mtm"]
        slot[f"{p}_trades"] = r["trades_started"]
        slot[f"{p}_blockers"] = r["blocker_count"]
        slot[f"{p}_close_rate"] = r["close_rate"]
    summary_by_coin = [by_coin[k] for k in sorted(by_coin)]

    leave = leave_one_coin_out(coin_rows)
    safety = safety_aggregate(trades, integrity)

    # first trade parity
    parity: list[dict[str, Any]] = []
    coins = sorted({str(t["coin"]).upper() for t in trades})
    for coin in coins:
        by_p = {
            p: [t for t in trades if t["coin"] == coin and t["profile"] == p]
            for p in manifest["profiles"]
        }
        parity.append(first_trade_parity_rows(by_p, coin=coin))

    common_start, common_end, covered = build_common_window(coin_meta)
    common_trades = []
    if common_start and common_end and covered:
        common_trades = filter_trades_common_window(
            trades,
            common_start=common_start,
            common_end=common_end,
            coins=set(covered),
        )
    common_by_cp: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for t in common_trades:
        common_by_cp[(str(t["coin"]).upper(), str(t["profile"]))].append(t)
    common_coin_rows = []
    for (coin, profile), group in sorted(common_by_cp.items()):
        meta = coin_meta.get(coin) or {}
        common_coin_rows.append(
            summarize_coin_profile(
                group,
                coin=coin,
                profile=profile,
                n_candles=int(meta.get("n_candles") or 0),
                first_ts=common_start,
                last_ts=common_end,
            )
        )
    common_summary = summarize_by_profile(common_coin_rows)
    for row in common_summary:
        row["common_start"] = common_start
        row["common_end"] = common_end
        row["n_coins_covered"] = len(covered)

    blocker_details = []
    for t in trades:
        if int(safe_float(t.get("is_blocker"))) != 1:
            continue
        blocker_details.append(
            {
                "coin": t.get("coin"),
                "profile": t.get("profile"),
                "trade_id": t.get("trade_id"),
                "start_bar": t.get("start_bar"),
                "end_bar": t.get("end_bar"),
                "duration_candles": t.get("duration_candles"),
                "max_cycle": t.get("max_cycle"),
                "realized_pnl": t.get("realized_pnl"),
                "open_mtm": t.get("open_mtm"),
                "total_pnl": t.get("total_pnl"),
                "pending_cycle_loss_usdt": t.get("pending_cycle_loss_usdt"),
                "filled_stages": t.get("filled_stages"),
                "planned_stages": t.get("planned_stages"),
                "distance_to_exit_pct": t.get("distance_to_exit_pct"),
                "blocker_root_cause": t.get("blocker_root_cause")
                or classify_blocker_root_cause(t),
            }
        )
    blocker_summary = []
    by_bp: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for b in blocker_details:
        by_bp[str(b["profile"])].append(b)
    for profile, group in sorted(by_bp.items()):
        causes: dict[str, int] = defaultdict(int)
        for b in group:
            causes[str(b.get("blocker_root_cause") or "other")] += 1
        blocker_summary.append(
            {
                "profile": profile,
                "blocker_count": len(group),
                "blocker_open_mtm": sum(safe_float(b["open_mtm"]) for b in group),
                "blocker_realized_pnl": sum(safe_float(b["realized_pnl"]) for b in group),
                "blocker_total_pnl": sum(safe_float(b["total_pnl"]) for b in group),
                "avg_duration": statistics.mean(
                    [int(safe_float(b["duration_candles"])) for b in group]
                )
                if group
                else None,
                "highest_cycle": max(
                    (int(safe_float(b["max_cycle"])) for b in group), default=0
                ),
                **{f"root_{k}": v for k, v in sorted(causes.items())},
            }
        )

    pnl_recon = []
    for t in trades:
        realized = safe_float(t.get("realized_pnl"))
        open_mtm = safe_float(t.get("open_mtm"))
        total = safe_float(t.get("total_pnl"))
        pnl_recon.append(
            {
                "trade_id": t.get("trade_id"),
                "coin": t.get("coin"),
                "profile": t.get("profile"),
                "realized_pnl": realized,
                "open_mtm": open_mtm,
                "total_pnl": total,
                "expected_total": realized + open_mtm,
                "delta": total - (realized + open_mtm),
                "pass": int(abs(total - (realized + open_mtm)) < 1e-6),
            }
        )

    worst = sorted(
        [t for t in trades if int(safe_float(t.get("trade_flat"))) == 1],
        key=lambda t: safe_float(t.get("realized_pnl")),
    )[:30]
    open_end = [t for t in trades if int(safe_float(t.get("trade_flat"))) != 1]

    write_csv(output_dir / "raw_profile_coin_runs.csv", coin_rows)
    write_csv(output_dir / "summary_by_profile.csv", summary_profile)
    write_csv(output_dir / "summary_by_coin.csv", summary_by_coin)
    write_csv(output_dir / "summary_by_profile_coin.csv", coin_rows)
    write_csv(output_dir / "summary_common_window.csv", common_summary)
    write_csv(output_dir / "summary_by_month.csv", monthly_summary(trades))
    write_csv(output_dir / "blocker_summary.csv", blocker_summary)
    write_csv(output_dir / "blocker_details.csv", blocker_details)
    write_csv(output_dir / "pnl_reconciliation.csv", pnl_recon)
    write_csv(output_dir / "exposure_concurrency.csv", exposure_concurrency_rows(trades))
    write_csv(output_dir / "leave_one_coin_out.csv", leave)
    write_csv(output_dir / "worst_trades.csv", worst)
    write_csv(output_dir / "open_trades_at_end.csv", open_end)
    write_csv(output_dir / "first_trade_parity.csv", parity)

    decision = decide_preliminary(
        summary_profile=summary_profile,
        leaveouts=leave,
        safety=safety,
        common_summary=common_summary,
    )
    atomic_write_json(output_dir / "safety.json", safety)
    atomic_write_json(
        output_dir / "integrity.json",
        {
            "n_integrity_rows": len(integrity),
            "n_fail": sum(1 for r in integrity if int(safe_float(r.get("pass"))) != 1),
            "all_pass": int(
                all(int(safe_float(r.get("pass"))) == 1 for r in integrity)
            )
            if integrity
            else 1,
            "first_trade_parity": parity,
            "common_window": {
                "start": common_start,
                "end": common_end,
                "coins": covered,
            },
        },
    )
    atomic_write_json(output_dir / "decision_preliminary.json", decision)
    write_report(
        output_dir / "REPORT.md",
        manifest=manifest,
        decision=decision,
        summary_profile=summary_profile,
        safety=safety,
        first_parity=parity,
    )
    return {"safety": safety, "decision": decision, "summary_profile": summary_profile}


def run_validation(
    *,
    output_dir: Path,
    profiles: Sequence[str],
    coins: Sequence[str] | None,
    start: str | None,
    end: str | None,
    initial_long: float,
    initial_short: float,
    warmup: int,
    candle_limit: int,
    mode: str,
    resume: bool,
    checkpoint_every: int,
    max_trades: int | None,
) -> dict[str, Any]:
    assert_output_dir_safe(output_dir, resume=resume)
    output_dir.mkdir(parents=True, exist_ok=True)

    source_coins, excluded, source_manifest = load_source_universe(SOURCE_LARGE)
    if mode == "smoke":
        selected = ["APTUSDT", "BTCUSDT", "ETHUSDT"]
    elif coins:
        selected = [c.upper() for c in coins]
    else:
        selected = list(source_coins)

    profiles_t = validate_profiles(profiles)
    inherited = inherited_config_payload(source_manifest)
    inherited["warmup"] = int(warmup)
    inherited["candle_limit"] = int(candle_limit)
    inherited["initial_long_usdt"] = float(initial_long)
    inherited["initial_short_usdt"] = float(initial_short)
    # Sizing still flows through LONG_NOTIONAL in run_isolated_blocker; document CLI intent.
    if abs(float(initial_long) - LONG_NOTIONAL) > 1e-9 or abs(
        float(initial_short) - SHORT_NOTIONAL
    ) > 1e-9:
        log(
            f"NOTE: CLI sizes {initial_long}/{initial_short} requested; "
            f"executor currently fixed at {LONG_NOTIONAL}/{SHORT_NOTIONAL} "
            "(same as source large run)."
        )

    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": mode,
        "profiles": list(profiles_t),
        "coins": selected,
        "n_coins": len(selected),
        "start_filter": start,
        "end_filter": end,
        "source_run": inherited["source_run"],
        "inherited": inherited,
        "git": _git_status(),
        "checkpoint_every": checkpoint_every,
        "max_trades": max_trades,
    }
    atomic_write_json(output_dir / "run_manifest.json", manifest)

    # Copy universe / excluded for this run
    write_csv(
        output_dir / "coin_universe.csv",
        [
            {
                "coin": c,
                "included": True,
                "source": "large_staging_run",
            }
            for c in selected
        ],
    )
    write_csv(output_dir / "excluded_coins.csv", excluded)

    ckpt_path = output_dir / "checkpoint.json"
    checkpoint = None
    if resume and ckpt_path.exists():
        checkpoint = load_checkpoint(ckpt_path)
    if not isinstance(checkpoint, dict):
        checkpoint = _empty_checkpoint(coins=selected, profiles=profiles_t)
    done = set(checkpoint.get("completed_keys") or [])

    trades_path = output_dir / "continuous_trades.csv"
    integ_path = output_dir / "continuous_overlap_integrity.csv"

    coin_meta: dict[str, dict[str, Any]] = {}
    t0 = time.time()
    completed = 0
    planned = len(selected) * len(profiles_t)

    for coin in selected:
        raw = load_candles_for_symbol(coin, limit=candle_limit)
        candles = normalize_candles(coin, raw)
        # Optional time filters by timestamp string
        if start or end:
            filtered = []
            for c in candles:
                ts = _ts_of(c)
                if start and ts < start:
                    continue
                if end and ts > end:
                    continue
                filtered.append(c)
            candles = filtered
        coin_meta[coin] = {
            "n_candles": len(candles),
            "first_timestamp": _ts_of(candles[0]) if candles else "",
            "last_timestamp": _ts_of(candles[-1]) if candles else "",
        }

        for profile in profiles_t:
            key = f"{coin}|{profile}"
            if key in done:
                log(f"skip completed {key}")
                completed += 1
                continue
            log(f"RUN {key} candles={len(candles)} warmup={warmup}")
            try:
                trades = run_continuous_sequence(
                    coin=coin,
                    profile=profile,
                    candles=candles,
                    warmup=warmup,
                    max_trades=max_trades,
                    capture_economics=True,
                )
                enriched = [enrich_trade_row(t, candles) for t in trades]
                integrity = check_overlap_integrity(enriched)
                for row in enriched:
                    _append_csv_row(trades_path, row, RAW_FIELDS)
                for row in integrity:
                    _append_csv_row(
                        integ_path,
                        row,
                        [
                            "coin",
                            "profile",
                            "trade_id",
                            "start_bar",
                            "end_bar",
                            "flat_bar",
                            "next_start_bar",
                            "overlap_detected",
                            "stale_orders_detected",
                            "active_position_at_next_start",
                            "duplicate_trade_id",
                            "pass",
                        ],
                    )
                done.add(key)
                checkpoint["completed_keys"] = sorted(done)
                checkpoint["updated_at"] = datetime.now(timezone.utc).isoformat()
                completed += 1
                if completed % max(1, checkpoint_every) == 0 or completed == planned:
                    atomic_write_json(ckpt_path, checkpoint)
                log(
                    f"DONE {key} trades={len(enriched)} "
                    f"flat={sum(1 for t in enriched if int(t.get('trade_flat') or 0)==1)} "
                    f"elapsed={time.time()-t0:.1f}s"
                )
            except Exception as exc:  # noqa: BLE001
                err = {
                    "key": key,
                    "error": str(exc),
                    "traceback": traceback.format_exc(),
                }
                checkpoint.setdefault("errors", []).append(err)
                atomic_write_json(ckpt_path, checkpoint)
                log(f"ERROR {key}: {exc}")
                raise

    atomic_write_json(ckpt_path, checkpoint)
    # Persist coin meta for rebuilds
    atomic_write_json(output_dir / "coin_meta.json", coin_meta)
    result = rebuild_artifacts(
        output_dir=output_dir, manifest=manifest, coin_meta=coin_meta
    )
    result["elapsed_sec"] = time.time() - t0
    result["completed"] = completed
    result["planned"] = planned
    manifest["elapsed_sec"] = result["elapsed_sec"]
    manifest["completed_keys"] = sorted(done)
    atomic_write_json(output_dir / "run_manifest.json", manifest)
    return result


def print_manual_commands(*, output_dir: Path, log_dir: Path, profiles: str) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    out = str(output_dir)
    logf = str(log_dir / "staging_profiles_continuous_1000_500_20260722.log")
    pidf = str(log_dir / "staging_profiles_continuous_1000_500_20260722.pid")
    cmd = (
        f"nohup python3 -m research.backtests.run_staging_profiles_continuous_validation "
        f"--mode full --resume --profiles {profiles} "
        f"--output-dir {out} --checkpoint-every 1 "
        f"> {logf} 2>&1 & echo $! > {pidf}"
    )
    print("Manual full-run command:")
    print(cmd)
    print(f"LOG={logf}")
    print(f"PID_FILE={pidf}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Continuous staging profile validation (legacy/TEM/adaptive_equal)"
    )
    parser.add_argument(
        "--profiles",
        default="legacy,two_early_medium,adaptive_equal",
    )
    parser.add_argument("--coins", default="", help="Comma-separated subset")
    parser.add_argument("--start", default=None)
    parser.add_argument("--end", default=None)
    parser.add_argument("--initial-long-usdt", type=float, default=LONG_NOTIONAL)
    parser.add_argument("--initial-short-usdt", type=float, default=SHORT_NOTIONAL)
    parser.add_argument("--warmup", type=int, default=DEFAULT_WARMUP)
    parser.add_argument("--candle-limit", type=int, default=FULL_HISTORY_CANDLE_LIMIT)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--checkpoint-every", type=int, default=1)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--log-dir", type=Path, default=DEFAULT_LOG_DIR)
    parser.add_argument("--mode", choices=("smoke", "full"), default="smoke")
    parser.add_argument("--max-trades", type=int, default=None)
    parser.add_argument("--print-manual-command-only", action="store_true")
    parser.add_argument("--rebuild-only", action="store_true")
    args = parser.parse_args()

    profiles = tuple(p.strip() for p in str(args.profiles).split(",") if p.strip())
    if args.print_manual_command_only:
        print_manual_commands(
            output_dir=args.output_dir, log_dir=args.log_dir, profiles=",".join(profiles)
        )
        return

    if args.rebuild_only:
        coin_meta = {}
        meta_path = args.output_dir / "coin_meta.json"
        if meta_path.exists():
            coin_meta = json.loads(meta_path.read_text(encoding="utf-8"))
        manifest = json.loads((args.output_dir / "run_manifest.json").read_text())
        result = rebuild_artifacts(
            output_dir=args.output_dir, manifest=manifest, coin_meta=coin_meta
        )
        log(json.dumps({"safety": result["safety"], "decision": result["decision"]}, indent=2))
        return

    coins = [c.strip() for c in str(args.coins).split(",") if c.strip()] or None
    # Document inherited config before start
    _, _, source_manifest = load_source_universe(SOURCE_LARGE)
    inherited = inherited_config_payload(source_manifest)
    log("=== Inherited config from large staging run ===")
    log(json.dumps(inherited, indent=2))

    result = run_validation(
        output_dir=args.output_dir,
        profiles=profiles,
        coins=coins,
        start=args.start,
        end=args.end,
        initial_long=args.initial_long_usdt,
        initial_short=args.initial_short_usdt,
        warmup=args.warmup,
        candle_limit=args.candle_limit,
        mode=args.mode,
        resume=args.resume,
        checkpoint_every=args.checkpoint_every,
        max_trades=args.max_trades,
    )
    log("=== Safety ===")
    log(json.dumps(result["safety"], indent=2))
    log("=== Decision ===")
    log(json.dumps(result["decision"], indent=2, default=str))
    if args.mode == "smoke" and int(result["safety"].get("all_green") or 0) == 1:
        print_manual_commands(
            output_dir=args.output_dir,
            log_dir=args.log_dir,
            profiles=",".join(profiles),
        )


if __name__ == "__main__":
    main()

"""Multi-coin price-staging profile grid (research-only).

Reuses isolated-blocker replay from ``multicoin_blocker_price_staging``.
Checkpoint/resume after each fully completed coin. No live/runtime mutation.
"""

from __future__ import annotations

import csv
import json
import os
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from research.backtests.candle_loader import load_candles_for_symbol
from research.backtests.historical_backtest import normalize_candles
from research.backtests.inventory_mtm_freeze import safe_float
from research.backtests.multicoin_blocker_price_staging import (
    APT_PROTOTYPE,
    DEFAULT_BASELINE,
    FULL_HISTORY_CANDLE_LIMIT,
    LONG_NOTIONAL,
    SHORT_NOTIONAL,
    analyze_blocker_run,
    check_apt_prototype_parity,
    classify_vs_legacy,
    run_isolated_blocker,
    summarize_profile,
)
from research.backtests.pnl_coverage_audit import build_pnl_coverage_audit
from research.backtests.recovery_reentry_policy import load_baseline_blockers
from research.backtests.second_leg_price_staging import (
    PROFILE_BUILDERS,
    SecondLegPriceStagingConfig,
    list_grid_profile_names,
    parse_profile_selection,
    profile_definitions_payload,
    resolve_profile,
)

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUT = ROOT / "research/backtests/results/multicoin_price_staging_grid_1000_500_20260721"
PROTECTED = (
    DEFAULT_BASELINE,
    ROOT / "research/backtests/results/safe_cycle_boundary_freeze_audit_20260720",
    ROOT / "research/backtests/results/long_baseline_1000_500_stage_tp_audit_20260721",
    ROOT / "research/backtests/results/apt_baseline_blocker_root_cause_20260721",
    ROOT / "research/backtests/results/apt_t3_stage_tp_size_comparison_20260721",
    ROOT / "research/backtests/results/second_leg_price_staging_code_audit_20260721",
    ROOT / "research/backtests/results/apt_t3_short_reduce_price_staging_lab_20260721",
    ROOT / "research/backtests/results/multicoin_blocker_price_staging_1000_500_20260721",
)

CYCLE_SR_RE = re.compile(r"^CYCLE_(\d+)_SHORT_REDUCE$")
APT_GATE_PROFILES = ("legacy", "linear4", "conservative3", "small_early4")


def log(msg: str) -> None:
    print(msg, flush=True)


def _result_fills(result: Any) -> list[dict[str, Any]]:
    """BacktestResult uses ``fill_log`` (historical alias ``fills_log`` tolerated)."""
    fills = getattr(result, "fill_log", None)
    if fills is None:
        fills = getattr(result, "fills_log", None)
    return list(fills or [])


def assert_output_dir_safe(output_dir: Path, *, resume: bool = False) -> None:
    resolved = output_dir.resolve()
    for protected in PROTECTED:
        if resolved == protected.resolve():
            raise RuntimeError(f"refusing protected output dir: {protected}")
    if resume:
        return
    if output_dir.exists() and any(output_dir.iterdir()):
        raise FileExistsError(f"refusing to overwrite non-empty output dir: {output_dir}")


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def atomic_write_json(path: Path, payload: Any) -> None:
    atomic_write_text(path, json.dumps(payload, indent=2, default=str) + "\n")


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        atomic_write_text(path, "")
        return
    fields: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for key in row:
            if key not in seen:
                seen.add(key)
                fields.append(key)
    from io import StringIO

    handle = StringIO()
    writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        out = {}
        for key in fields:
            val = row.get(key)
            out[key] = json.dumps(val, default=str) if isinstance(val, (dict, list)) else val
        writer.writerow(out)
    atomic_write_text(path, handle.getvalue())


def load_csv_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _git() -> dict[str, Any]:
    status: dict[str, Any] = {"commit": None, "dirty": None}
    try:
        status["commit"] = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip()
        porcelain = subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True)
        status["dirty"] = bool(porcelain.strip())
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        status["error"] = str(exc)
    return status


def detect_stage_safety(result: Any) -> dict[str, int]:
    """Over-close / duplicate-stage guards from SHORT_REDUCE fills + intents."""
    fill_keys: dict[tuple[int, int], int] = {}
    qty_by_cycle: dict[int, float] = {}
    for fill in _result_fills(result):
        purpose = str(fill.get("purpose") or "")
        m = CYCLE_SR_RE.match(purpose)
        if not m:
            continue
        meta = dict(fill.get("metadata_excerpt") or {})
        if not (meta.get("research_price_staging") or meta.get("is_staged_second_leg_tp")):
            continue
        cycle = int(m.group(1))
        stage = int(meta.get("stage_index") or 0)
        fill_keys[(cycle, stage)] = fill_keys.get((cycle, stage), 0) + 1
        qty_by_cycle[cycle] = qty_by_cycle.get(cycle, 0.0) + safe_float(fill.get("qty"))

    intent_qty: dict[int, float] = {}
    for intent in result.intent_log or []:
        purpose = str(intent.get("purpose") or "")
        m = CYCLE_SR_RE.match(purpose)
        if not m:
            continue
        meta = dict(intent.get("metadata_excerpt") or {})
        if not (meta.get("research_price_staging") or meta.get("is_staged_second_leg_tp")):
            continue
        cycle = int(m.group(1))
        intent_qty[cycle] = intent_qty.get(cycle, 0.0) + safe_float(intent.get("qty"))

    duplicate_stage = sum(1 for n in fill_keys.values() if n > 1)
    over_close = 0
    for cycle, filled in qty_by_cycle.items():
        planned = intent_qty.get(cycle)
        if planned is None:
            continue
        if filled > planned + max(1e-6, 0.001 * planned):
            over_close += 1
    return {"duplicate_stage": int(duplicate_stage), "over_close": int(over_close)}


def extract_undercoverage_cases(
    *,
    coin: str,
    profile: str,
    trade_number: int,
    result: Any,
    row: dict[str, Any],
) -> list[dict[str, Any]]:
    if int(row.get("undercoverage") or 0) <= 0:
        return []
    cases: list[dict[str, Any]] = []
    audit_rows = build_pnl_coverage_audit(result)
    staged_fills = []
    for fill in _result_fills(result):
        purpose = str(fill.get("purpose") or "")
        m = CYCLE_SR_RE.match(purpose)
        if not m:
            continue
        meta = dict(fill.get("metadata_excerpt") or {})
        if meta.get("research_price_staging") or meta.get("is_staged_second_leg_tp"):
            staged_fills.append((int(m.group(1)), int(meta.get("stage_index") or 0), fill))

    for audit in audit_rows:
        if "undercover" not in str(audit.get("status") or "").lower():
            continue
        cycle = int(audit.get("cycle_index") or 0)
        cycle_stages = [s for s in staged_fills if s[0] == cycle]
        last_stage = max((s[1] for s in cycle_stages), default=None)
        required = abs(safe_float(audit.get("loss_pnl")))
        realized = safe_float(audit.get("cover_pnl"))
        rest_cov = safe_float(audit.get("missing_pnl"))
        rest_qty = safe_float(audit.get("qty_shortfall"))
        min_notional_status = "fallback_single" if int(row.get("fallback_single_stage") or 0) else "ok"
        if int(row.get("staging_activated") or 0) == 0 and str(profile) != "legacy":
            min_notional_status = "min_notional_or_reduced"
        cases.append(
            {
                "coin": coin,
                "profile": profile,
                "trade": int(trade_number),
                "cycle": cycle,
                "required_coverage": required,
                "realized_coverage": realized,
                "rest_coverage": rest_cov,
                "rest_qty": rest_qty,
                "last_stage": last_stage,
                "min_notional_rounding_status": min_notional_status,
                "status_at_series_end": row.get("status"),
                "coverage_gate_state": audit.get("status"),
                "loss_purpose": audit.get("loss_purpose"),
                "cover_purpose": audit.get("cover_purpose"),
            }
        )
    return cases


def empty_checkpoint(*, profiles: list[str], coins: list[str]) -> dict[str, Any]:
    return {
        "version": 1,
        "profiles": list(profiles),
        "coins": list(coins),
        "completed_coins": [],
        "completed_keys": [],  # ["COIN|profile", ...]
        "updated_at": None,
    }


def load_checkpoint(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def completed_key(coin: str, profile: str) -> str:
    return f"{coin.upper()}|{profile}"


def run_apt_gate_once(
    *,
    candles: list[Any],
    baseline_row: dict[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """APT T3 S1000 prototype gate using lab profile names (once per run)."""
    by_profile: dict[str, dict[str, Any]] = {}
    for name in APT_GATE_PROFILES:
        cfg = PROFILE_BUILDERS[name]()
        result = run_isolated_blocker(
            coin="APTUSDT",
            candles=candles,
            start_index=int(APT_PROTOTYPE["start_index"]),
            staging_config=cfg,
            trade_number=int(APT_PROTOTYPE["trade_number"]),
        )
        row = analyze_blocker_run(
            coin="APTUSDT",
            trade_number=int(APT_PROTOTYPE["trade_number"]),
            start_index=int(APT_PROTOTYPE["start_index"]),
            profile=name,
            result=result,
            candles=candles,
            baseline_row=baseline_row,
        )
        by_profile[name] = row
        log(
            f"[grid] APT-gate {name}: flat={row['trade_flat']} mtm={row['final_mtm']:.4f} "
            f"reach={row.get('apt_bounce_reaches')}"
        )
    return check_apt_prototype_parity(by_profile), by_profile


def classify_profile_safety(
    summary: dict[str, Any],
    *,
    apt_ok: bool,
    over_close_sum: int,
    duplicate_stage_sum: int,
) -> dict[str, Any]:
    invalid = int(summary.get("invalid_partial_sum") or 0)
    under = int(summary.get("undercoverage_sum") or 0)
    neg_closed = int(summary.get("closed_negative") or 0)
    binding_ok = (
        apt_ok
        and invalid == 0
        and under == 0
        and over_close_sum == 0
        and duplicate_stage_sum == 0
    )
    return {
        "profile": summary.get("profile"),
        "invalid_partial": invalid,
        "undercoverage": under,
        "negative_closed": neg_closed,
        "over_close": over_close_sum,
        "duplicate_stage": duplicate_stage_sum,
        "apt_control_ok": int(apt_ok),
        "safety_valid": int(binding_ok),
    }


def write_report(
    path: Path,
    *,
    summaries: list[dict[str, Any]],
    safety_rows: list[dict[str, Any]],
    apt_parity: dict[str, Any],
    guards: dict[str, Any],
    n_blockers: int,
) -> None:
    lines = [
        "# Multi-coin price-staging grid @1000/500",
        "",
        "Research-only isolated blocker replay. No live recommendation.",
        "",
        f"Population: **{n_blockers}** baseline blockers.",
        f"Size: **{int(LONG_NOTIONAL)}/{int(SHORT_NOTIONAL)}** USDT.",
        f"APT gate ok: **{apt_parity.get('ok')}**",
        "",
        "## Safety",
        "",
        "| profile | safety_valid | invalid | undercov | neg_closed | over_close | dup_stage |",
        "|---|---|---|---|---|---|---|",
    ]
    for s in safety_rows:
        lines.append(
            "| {profile} | {safety_valid} | {invalid_partial} | {undercoverage} | "
            "{negative_closed} | {over_close} | {duplicate_stage} |".format(**s)
        )
    lines.extend(
        [
            "",
            "## Profile performance",
            "",
            "| profile | closed | open | +closes | total_mtm | worst | undercov |",
            "|---|---|---|---|---|---|---|",
        ]
    )
    for s in summaries:
        lines.append(
            "| {profile} | {closed}/{n} | {still_open} | {closed_positive} | "
            "{total_mtm:.2f} | {worst_final_mtm} | {undercoverage_sum} |".format(
                n=n_blockers, **{k: v for k, v in s.items() if k != "n_blockers"}
            )
        )
    lines.extend(["", "## Guards", "", "```json", json.dumps(guards, indent=2, default=str), "```", ""])
    atomic_write_text(path, "\n".join(lines) + "\n")


def _dedupe_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_key: dict[tuple[str, str], dict[str, Any]] = {}
    order: list[tuple[str, str]] = []
    for row in rows:
        key = (str(row.get("coin") or "").upper(), str(row.get("profile") or ""))
        if key not in by_key:
            order.append(key)
        by_key[key] = row
    return [by_key[k] for k in order]


def finalize_artifacts(
    output_dir: Path,
    *,
    per_coin: list[dict[str, Any]],
    undercoverage_cases: list[dict[str, Any]],
    apt_parity: dict[str, Any],
    profile_names: list[str],
    n_blockers: int,
) -> dict[str, Any]:
    per_coin = _dedupe_rows(per_coin)
    summaries = [summarize_profile([r for r in per_coin if r.get("profile") == n], profile=n) for n in profile_names]

    safety_rows: list[dict[str, Any]] = []
    for name in profile_names:
        rows = [r for r in per_coin if r.get("profile") == name]
        summary = next(s for s in summaries if s["profile"] == name)
        over = sum(int(r.get("over_close") or 0) for r in rows)
        dup = sum(int(r.get("duplicate_stage") or 0) for r in rows)
        safety_rows.append(
            classify_profile_safety(
                summary,
                apt_ok=bool(apt_parity.get("ok")),
                over_close_sum=over,
                duplicate_stage_sum=dup,
            )
        )

    ranking_base = []
    for summary, safety in zip(summaries, safety_rows):
        ranking_base.append({**summary, **safety})

    safety_valid = sorted(
        [r for r in ranking_base if int(r.get("safety_valid") or 0)],
        key=lambda r: (
            -int(r.get("closed") or 0),
            -int(r.get("closed_positive") or 0),
            -safe_float(r.get("total_mtm")),
            safe_float(r.get("worst_final_mtm") or 0),
        ),
    )
    exploratory = sorted(
        ranking_base,
        key=lambda r: (
            -int(r.get("closed") or 0),
            -safe_float(r.get("total_mtm")),
            int(r.get("undercoverage") or 0),
            safe_float(r.get("worst_final_mtm") or 0),
        ),
    )

    stage_fill = [
        {
            "coin": r.get("coin"),
            "profile": r.get("profile"),
            "planned_stages": r.get("planned_stages"),
            "filled_stages": r.get("filled_stages"),
            "staging_activated": r.get("staging_activated"),
            "fallback_single_stage": r.get("fallback_single_stage"),
            "distinct_triggers": r.get("distinct_triggers"),
        }
        for r in per_coin
    ]
    exit_drop = [
        {
            "coin": r.get("coin"),
            "profile": r.get("profile"),
            "exit_before_first_stage": r.get("exit_before_first_stage"),
            "exit_after_first_stage": r.get("exit_after_first_stage"),
            "strongest_exit_drop": r.get("strongest_exit_drop"),
            "bounce_reaches_exit": r.get("bounce_reaches_exit"),
        }
        for r in per_coin
    ]
    exposure = [
        {
            "coin": r.get("coin"),
            "profile": r.get("profile"),
            "gross_exposure": r.get("gross_exposure"),
            "net_exposure": r.get("net_exposure"),
        }
        for r in per_coin
    ]
    duration = [
        {
            "coin": r.get("coin"),
            "profile": r.get("profile"),
            "duration_candles": r.get("duration_candles"),
            "trade_flat": r.get("trade_flat"),
        }
        for r in per_coin
    ]

    guards = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "git": _git(),
        "n_blockers": n_blockers,
        "apt_parity_ok": apt_parity.get("ok"),
        "invalid_partial_total": sum(int(r.get("invalid_partial") or 0) for r in per_coin),
        "undercoverage_total": sum(int(r.get("undercoverage") or 0) for r in per_coin),
        "over_close_total": sum(int(r.get("over_close") or 0) for r in per_coin),
        "duplicate_stage_total": sum(int(r.get("duplicate_stage") or 0) for r in per_coin),
        "negative_closed_total": sum(
            1
            for r in per_coin
            if int(r.get("trade_flat") or 0) and safe_float(r.get("final_mtm")) <= 0
        ),
        "safety_binding": [
            "apt_parity",
            "invalid_partial==0",
            "undercoverage==0",
            "over_close==0",
            "duplicate_stage==0",
        ],
    }

    write_csv(output_dir / "profile_summary.csv", summaries)
    write_csv(output_dir / "safety_valid_ranking.csv", safety_valid)
    write_csv(output_dir / "exploratory_ranking.csv", exploratory)
    write_csv(output_dir / "per_coin_per_profile.csv", per_coin)
    write_csv(output_dir / "partial_per_coin_per_profile.csv", per_coin)
    write_csv(output_dir / "undercoverage_cases.csv", undercoverage_cases)
    write_csv(output_dir / "stage_fill_summary.csv", stage_fill)
    write_csv(output_dir / "exit_drop_summary.csv", exit_drop)
    write_csv(output_dir / "exposure_summary.csv", exposure)
    write_csv(output_dir / "duration_summary.csv", duration)
    atomic_write_json(output_dir / "guards.json", guards)
    atomic_write_json(output_dir / "apt_parity.json", apt_parity)
    atomic_write_text(
        output_dir / "profile_definitions.yaml",
        yaml.safe_dump(profile_definitions_payload(), sort_keys=False),
    )
    write_report(
        output_dir / "REPORT.md",
        summaries=summaries,
        safety_rows=safety_rows,
        apt_parity=apt_parity,
        guards=guards,
        n_blockers=n_blockers,
    )
    return {"summaries": summaries, "safety_rows": safety_rows, "guards": guards}


def run_grid(
    *,
    baseline_dir: Path,
    profiles_spec: str,
    output_dir: Path,
    candle_limit: int = FULL_HISTORY_CANDLE_LIMIT,
    coins_filter: list[str] | None = None,
    max_coins: int | None = None,
    resume: bool = False,
    skip_apt_gate: bool = False,
) -> dict[str, Any]:
    assert_output_dir_safe(output_dir, resume=resume)
    output_dir.mkdir(parents=True, exist_ok=True)

    profiles = parse_profile_selection(profiles_spec)
    profile_names = [p.profile_name for p in profiles]
    if "legacy" not in profile_names:
        # Always include legacy control for vs-legacy classification.
        profiles = [resolve_profile("legacy"), *profiles]
        profile_names = [p.profile_name for p in profiles]

    blockers = load_baseline_blockers(baseline_dir / "blocker_trades.csv")
    if coins_filter:
        wanted = {c.upper() for c in coins_filter}
        blockers = [b for b in blockers if str(b.get("coin") or "").upper() in wanted]
    if max_coins is not None:
        blockers = blockers[: int(max_coins)]
    if not blockers:
        raise RuntimeError("no blockers selected")

    coins = [str(b["coin"]).upper() for b in blockers]
    checkpoint_path = output_dir / "checkpoint.json"
    partial_path = output_dir / "partial_per_coin_per_profile.csv"
    under_partial_path = output_dir / "undercoverage_cases.csv"

    checkpoint = empty_checkpoint(profiles=profile_names, coins=coins)
    per_coin: list[dict[str, Any]] = []
    undercoverage_cases: list[dict[str, Any]] = []
    completed: set[str] = set()

    if resume:
        loaded = load_checkpoint(checkpoint_path)
        if loaded:
            checkpoint = loaded
            completed = set(str(x) for x in (loaded.get("completed_keys") or []))
            per_coin = load_csv_rows(partial_path)
            undercoverage_cases = load_csv_rows(under_partial_path)
            # Drop rows for profiles not in this run selection, keep completed keys only for selected.
            per_coin = [r for r in per_coin if str(r.get("profile")) in profile_names]
            per_coin = _dedupe_rows(per_coin)
            log(f"[grid] resume: {len(completed)} completed coin|profile keys loaded")

    # Candles once per coin
    log(f"[grid] loading candles for {len(set(coins))} unique coins...")
    coin_candles: dict[str, list[Any]] = {}
    for coin in sorted(set(coins)):
        coin_candles[coin] = normalize_candles(
            coin, load_candles_for_symbol(coin, limit=int(candle_limit))
        )

    apt_candles = coin_candles.get("APTUSDT")
    if apt_candles is None:
        apt_candles = normalize_candles(
            "APTUSDT", load_candles_for_symbol("APTUSDT", limit=int(candle_limit))
        )
        coin_candles["APTUSDT"] = apt_candles

    apt_row = next(
        (b for b in blockers if str(b["coin"]).upper() == "APTUSDT"),
        {"coin": "APTUSDT", "trade_number": 3, "mtm_pnl": "", "status": "open"},
    )

    apt_parity_path = output_dir / "apt_parity.json"
    if resume and apt_parity_path.exists() and not skip_apt_gate:
        apt_parity = json.loads(apt_parity_path.read_text(encoding="utf-8"))
        log(f"[grid] resume: reused apt_parity.json ok={apt_parity.get('ok')}")
    elif skip_apt_gate:
        apt_parity = {"ok": True, "skipped": True}
    else:
        log("[grid] APT T3 prototype parity gate (once)...")
        apt_parity, _ = run_apt_gate_once(candles=apt_candles, baseline_row=apt_row)
        atomic_write_json(apt_parity_path, apt_parity)
        if not apt_parity.get("ok"):
            atomic_write_json(
                output_dir / "ABORT.json",
                {"reason": "APT prototype parity failed", "apt_parity": apt_parity},
            )
            log("ABORT: APT prototype parity failed")
            return {"aborted": True, "apt_parity": apt_parity, "output_dir": str(output_dir)}

    total_jobs = len(blockers) * len(profiles)
    done_jobs = len(completed)
    legacy_by_coin: dict[str, dict[str, Any]] = {}
    for row in per_coin:
        if row.get("profile") == "legacy":
            legacy_by_coin[str(row.get("coin") or "").upper()] = row

    for blocker in blockers:
        coin = str(blocker["coin"]).upper()
        trade_number = int(blocker["trade_number"])
        start_index = int(blocker["start_index"])
        candles = coin_candles[coin]
        coin_finished = True

        for cfg in profiles:
            key = completed_key(coin, cfg.profile_name)
            if key in completed:
                continue

            t0 = time.perf_counter()
            log(
                f"[grid] START coin={coin} profile={cfg.profile_name} "
                f"trade={trade_number} progress={done_jobs}/{total_jobs}"
            )
            result = run_isolated_blocker(
                coin=coin,
                candles=candles,
                start_index=start_index,
                staging_config=cfg,
                trade_number=trade_number,
            )
            row = analyze_blocker_run(
                coin=coin,
                trade_number=trade_number,
                start_index=start_index,
                profile=cfg.profile_name,
                result=result,
                candles=candles,
                baseline_row=blocker,
            )
            safety = detect_stage_safety(result)
            row.update(safety)

            if cfg.profile_name == "legacy":
                legacy_by_coin[coin] = row
                row["improvement_usdt"] = 0.0
                row["classification"] = "legacy_control"
            else:
                legacy = legacy_by_coin.get(coin)
                if legacy is None:
                    # Should not happen if legacy is first; compute once.
                    leg_cfg = resolve_profile("legacy")
                    leg_res = run_isolated_blocker(
                        coin=coin,
                        candles=candles,
                        start_index=start_index,
                        staging_config=leg_cfg,
                        trade_number=trade_number,
                    )
                    legacy = analyze_blocker_run(
                        coin=coin,
                        trade_number=trade_number,
                        start_index=start_index,
                        profile="legacy",
                        result=leg_res,
                        candles=candles,
                        baseline_row=blocker,
                    )
                    legacy.update(detect_stage_safety(leg_res))
                    legacy_by_coin[coin] = legacy
                row.update(classify_vs_legacy(staged=row, legacy=legacy))

            under_rows = extract_undercoverage_cases(
                coin=coin,
                profile=cfg.profile_name,
                trade_number=trade_number,
                result=result,
                row=row,
            )
            undercoverage_cases.extend(under_rows)

            # Replace any prior row for this key then append
            per_coin = [
                r
                for r in per_coin
                if not (
                    str(r.get("coin") or "").upper() == coin
                    and str(r.get("profile") or "") == cfg.profile_name
                )
            ]
            per_coin.append(row)
            completed.add(key)
            done_jobs += 1
            elapsed = time.perf_counter() - t0
            status = "closed" if int(row.get("trade_flat") or 0) else "open"
            log(
                f"[grid] END coin={coin} profile={cfg.profile_name} "
                f"duration_s={elapsed:.1f} status={status} "
                f"closed={int(row.get('trade_flat') or 0)} "
                f"stage_fills={row.get('filled_stages')} "
                f"undercoverage={row.get('undercoverage')} "
                f"progress={done_jobs}/{total_jobs}"
            )

        # Coin complete when all selected profiles done
        for cfg in profiles:
            if completed_key(coin, cfg.profile_name) not in completed:
                coin_finished = False
                break
        if coin_finished:
            completed_coins = list(dict.fromkeys([*(checkpoint.get("completed_coins") or []), coin]))
            checkpoint = {
                "version": 1,
                "profiles": profile_names,
                "coins": coins,
                "completed_coins": completed_coins,
                "completed_keys": sorted(completed),
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "done_jobs": done_jobs,
                "total_jobs": total_jobs,
            }
            write_csv(partial_path, _dedupe_rows(per_coin))
            write_csv(under_partial_path, undercoverage_cases)
            atomic_write_json(checkpoint_path, checkpoint)
            log(f"[grid] checkpoint saved after coin={coin} ({done_jobs}/{total_jobs})")

    payload = finalize_artifacts(
        output_dir,
        per_coin=per_coin,
        undercoverage_cases=undercoverage_cases,
        apt_parity=apt_parity,
        profile_names=profile_names,
        n_blockers=len(blockers),
    )
    checkpoint = {
        "version": 1,
        "profiles": profile_names,
        "coins": coins,
        "completed_coins": list(dict.fromkeys(coins)),
        "completed_keys": sorted(completed),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "done_jobs": done_jobs,
        "total_jobs": total_jobs,
        "finished": True,
    }
    atomic_write_json(checkpoint_path, checkpoint)
    atomic_write_json(
        output_dir / "run_manifest.json",
        {
            "output_dir": str(output_dir),
            "profiles": profile_names,
            "n_blockers": len(blockers),
            "apt_parity_ok": apt_parity.get("ok"),
            "aborted": False,
        },
    )
    log(f"[grid] wrote {output_dir}")
    return {"aborted": False, "apt_parity": apt_parity, "output_dir": str(output_dir), **payload}


def parse_sizes(spec: str) -> tuple[float, float]:
    raw = str(spec or "").strip()
    if ":" not in raw:
        raise ValueError(f"sizes must be LONG:SHORT, got {spec!r}")
    left, right = raw.split(":", 1)
    return float(left), float(right)


__all__ = [
    "DEFAULT_OUT",
    "assert_output_dir_safe",
    "atomic_write_json",
    "list_grid_profile_names",
    "log",
    "parse_profile_selection",
    "parse_sizes",
    "run_grid",
]

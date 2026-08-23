"""Orchestrator for multicoin frozen validation modes."""

from __future__ import annotations

import csv
import json
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..mfe_runner import _git_meta
from .checkpoint import (
    IncompatibleCheckpointError,
    ensure_dirs,
    list_complete_symbols,
    load_checkpoint,
    symbols_to_process,
    write_coin_checkpoint,
    write_coin_failure,
)
from .coin_backtest import run_one_coin
from .constants import (
    CODE_STATUS,
    DEFAULT_END,
    DEFAULT_OUTPUT_DIR,
    DEFAULT_START,
    DEFAULT_SYMBOLS_FILE,
    ELIGIBILITY_MEANS_THRESHOLD_PASS_NOT_COMPLETE_COVERAGE,
    ELIGIBILITY_THRESHOLDS,
    ELIGIBLE_CORE_30D,
    ENTRY_RULE,
    FUNDING_STATUS,
    MAIN_ELIGIBILITY,
    MIN_ELIGIBLE_FOR_ROBUST,
    NOTIONAL_USDT,
    PRIMARY_CELLS,
    PRIMARY_COST_PCT,
    SAME_BAR_RULE,
    SECONDARY_STRATEGIES,
    VERDICT_FAILED,
    VERDICT_INSUFFICIENT,
)
from .coverage import partition_by_class, probe_symbol_coverage, select_eligible_for_main
from .report import build_reports
from .resources import resource_snapshot
from .universe import apply_limit_symbols, audit_universe, load_universe, universe_hash
from .xrp_parity import frozen_cells_match_xrp_matrix_defs


def _utc(dt: datetime | str) -> datetime:
    if isinstance(dt, str):
        dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[5]


def _resolve(path: str | Path, repo: Path) -> Path:
    p = Path(path)
    return p if p.is_absolute() else repo / p


def write_manifest(paths: dict[str, Path], payload: dict[str, Any]) -> Path:
    path = paths["root"] / "run_manifest.json"
    path.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")
    return path


def run_dry_run(cfg: dict[str, Any]) -> dict[str, Any]:
    """Validate config / write plan — no ClickHouse, no market data."""
    repo = _repo_root()
    symbols_file = _resolve(cfg["symbols_file"], repo)
    out_dir = _resolve(cfg["output_dir"], repo)
    paths = ensure_dirs(out_dir)
    uni = load_universe(symbols_file)
    symbols = apply_limit_symbols(uni["symbols"], cfg.get("limit_symbols"))
    audit = audit_universe(uni["symbols"], expected_n=51)
    plan = {
        "mode": "dry-run",
        "code_status": CODE_STATUS,
        "symbols_file": str(symbols_file),
        "output_dir": str(out_dir),
        "start": _utc(cfg["start"]).isoformat(),
        "end": _utc(cfg["end"]).isoformat(),
        "n_symbols_in_universe": len(uni["symbols"]),
        "n_symbols_planned": len(symbols),
        "symbols_planned": symbols,
        "universe_audit_ok": audit["ok"],
        "universe_hash": audit["universe_hash"],
        "primary_cells": list(PRIMARY_CELLS),
        "secondary": list(SECONDARY_STRATEGIES),
        "entry_rule": ENTRY_RULE,
        "same_bar_rule": SAME_BAR_RULE,
        "funding_status": FUNDING_STATUS,
        "notional_usdt": NOTIONAL_USDT,
        "primary_cost_pct": PRIMARY_COST_PCT,
        "eligibility_means_threshold_pass_not_complete_coverage": (
            ELIGIBILITY_MEANS_THRESHOLD_PASS_NOT_COMPLETE_COVERAGE
        ),
        "eligibility_thresholds": dict(ELIGIBILITY_THRESHOLDS),
        "max_workers": cfg.get("max_workers", 1),
        "clickhouse_queries": False,
        "xrp_frozen_defs": frozen_cells_match_xrp_matrix_defs(),
        "git": _git_meta(repo),
        "resources_before": resource_snapshot(),
    }
    write_manifest(paths, {**plan, "cli_argv": cfg.get("cli_argv"), "started_at": datetime.now(timezone.utc).isoformat()})
    (paths["preflight"] / "dry_run_plan.json").write_text(json.dumps(plan, indent=2, default=str) + "\n")
    return {"ok": True, "plan": plan, "output_dir": str(out_dir), "verdict": CODE_STATUS}


def run_preflight(cfg: dict[str, Any]) -> dict[str, Any]:
    from ....cluster_sweep_research.clickhouse_source import default_client

    repo = _repo_root()
    symbols_file = _resolve(cfg["symbols_file"], repo)
    out_dir = _resolve(cfg["output_dir"], repo)
    paths = ensure_dirs(out_dir)
    start, end = _utc(cfg["start"]), _utc(cfg["end"])
    uni = load_universe(symbols_file)
    symbols = apply_limit_symbols(uni["symbols"], cfg.get("limit_symbols"))
    audit = audit_universe(uni["symbols"], expected_n=51)

    resources_before = resource_snapshot()
    rows: list[dict[str, Any]] = []
    client = default_client()
    try:
        for sym in symbols:
            try:
                rows.append(probe_symbol_coverage(client, sym, start, end))
            except Exception as exc:
                rows.append(
                    {
                        "symbol": sym,
                        "coverage_class": "INELIGIBLE_CORE",
                        "eligible_main": False,
                        "error": str(exc),
                        "performance_used_for_eligibility": False,
                    }
                )
    finally:
        if hasattr(client, "close"):
            client.close()

    parts = partition_by_class(rows)
    eligible = select_eligible_for_main(rows)
    preflight_summary = {
        "n_symbols": len(symbols),
        "n_eligible_core_30d": len(eligible),
        "partition": {k: len(v) for k, v in parts.items()},
        "eligible_symbols": eligible,
        "partial_symbols": parts.get("ELIGIBLE_CORE_PARTIAL", []),
        "insufficient_multicoin_coverage": len(eligible) < MIN_ELIGIBLE_FOR_ROBUST,
        "main_eligibility": MAIN_ELIGIBILITY,
        "performance_filter_applied": False,
    }

    (paths["preflight"] / "universe_audit.json").write_text(json.dumps(audit, indent=2) + "\n")
    (paths["preflight"] / "eligible_coins.json").write_text(
        json.dumps(
            {
                "eligible_class": ELIGIBLE_CORE_30D,
                "symbols": eligible,
                "partial": parts.get("ELIGIBLE_CORE_PARTIAL", []),
                "ineligible": parts.get("INELIGIBLE_CORE", []),
                "listing_limited": parts.get("LISTING_LIMITED", []),
            },
            indent=2,
        )
        + "\n"
    )
    (paths["preflight"] / "preflight_summary.json").write_text(json.dumps(preflight_summary, indent=2) + "\n")

    if rows:
        keys = sorted({k for r in rows for k in r.keys() if k != "feeds_raw"})
        with (paths["preflight"] / "coverage_by_coin.csv").open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
            w.writeheader()
            for r in rows:
                w.writerow({k: r.get(k) for k in keys})

    manifest = {
        "mode": "preflight-only",
        "cli_argv": cfg.get("cli_argv"),
        "started_at": cfg.get("started_at"),
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "git": _git_meta(repo),
        "universe_hash": universe_hash(uni["symbols"]),
        "window": {"start": start.isoformat(), "end": end.isoformat()},
        "strategies": {"primary": PRIMARY_CELLS, "secondary": SECONDARY_STRATEGIES},
        "cost_pct": PRIMARY_COST_PCT,
        "notional_usdt": NOTIONAL_USDT,
        "same_bar_rule": SAME_BAR_RULE,
        "entry_rule": ENTRY_RULE,
        "eligibility_means_threshold_pass_not_complete_coverage": (
            ELIGIBILITY_MEANS_THRESHOLD_PASS_NOT_COMPLETE_COVERAGE
        ),
        "eligibility_thresholds": dict(ELIGIBILITY_THRESHOLDS),
        "funding_status": FUNDING_STATUS,
        "preflight_summary": preflight_summary,
        "resources_before": resources_before,
        "resources_after": resource_snapshot(),
        "xrp_frozen_defs": frozen_cells_match_xrp_matrix_defs(),
    }
    write_manifest(paths, manifest)
    (paths["root"] / "resource_report.json").write_text(
        json.dumps({"before": resources_before, "after": manifest["resources_after"]}, indent=2) + "\n"
    )
    return {
        "ok": True,
        "output_dir": str(out_dir),
        "preflight_summary": preflight_summary,
        "verdict": VERDICT_INSUFFICIENT if preflight_summary["insufficient_multicoin_coverage"] else "PREFLIGHT_READY",
    }


def _load_preflight_eligible(paths: dict[str, Path]) -> list[str]:
    p = paths["preflight"] / "eligible_coins.json"
    if not p.exists():
        raise FileNotFoundError(f"Missing preflight eligible list: {p}. Run --preflight-only first.")
    data = json.loads(p.read_text(encoding="utf-8"))
    return [str(s).upper() for s in data.get("symbols") or []]


def _coverage_class_map(paths: dict[str, Path]) -> dict[str, str]:
    csv_path = paths["preflight"] / "coverage_by_coin.csv"
    out: dict[str, str] = {}
    if not csv_path.exists():
        return out
    with csv_path.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            out[str(row.get("symbol", "")).upper()] = str(row.get("coverage_class") or "")
    return out


def _process_symbol(
    symbol: str,
    *,
    start: datetime,
    end: datetime,
    coverage_class: str | None,
    paths: dict[str, Path],
    repo: Path,
) -> dict[str, Any]:
    from ....cluster_sweep_research.clickhouse_source import default_client

    client = default_client()
    try:
        result = run_one_coin(
            client,
            symbol=symbol,
            start=start,
            end=end,
            coverage_class=coverage_class,
            repo=repo,
            enforce_xrp_parity=True,
        )
        status = str(result.get("status") or "COMPLETE")
        write_coin_checkpoint(paths["checkpoints"], symbol=symbol, status=status, payload=result)
        if status == "FAILED_PARITY":
            write_coin_failure(
                paths["failures"],
                symbol=symbol,
                error="FAILED_PARITY",
                detail={"parity": result.get("parity")},
            )
            return {"symbol": symbol, "status": "FAILED_PARITY", "parity": result.get("parity")}
        return {"symbol": symbol, "status": status, "n_trades": result.get("n_trades")}
    except Exception as exc:
        detail = {"traceback": traceback.format_exc()}
        write_coin_failure(paths["failures"], symbol=symbol, error=str(exc), detail=detail)
        write_coin_checkpoint(
            paths["checkpoints"],
            symbol=symbol,
            status="FAILED",
            payload={"error": str(exc), "detail": detail},
        )
        return {"symbol": symbol, "status": "FAILED", "error": str(exc)}
    finally:
        if hasattr(client, "close"):
            client.close()


def run_backtest(cfg: dict[str, Any], *, resume: bool) -> dict[str, Any]:
    repo = _repo_root()
    out_dir = _resolve(cfg["output_dir"], repo)
    paths = ensure_dirs(out_dir)
    start, end = _utc(cfg["start"]), _utc(cfg["end"])
    resources_before = resource_snapshot()
    started = datetime.now(timezone.utc)

    eligible = _load_preflight_eligible(paths)
    if cfg.get("limit_symbols") is not None:
        eligible = eligible[: int(cfg["limit_symbols"])]
    class_map = _coverage_class_map(paths)

    if len(eligible) < MIN_ELIGIBLE_FOR_ROBUST:
        summary = build_reports(
            reports_dir=paths["reports"],
            trades=[],
            coin_stats=[],
            n_eligible=len(eligible),
            start=start,
            end=end,
            insufficient_coverage=True,
        )
        write_manifest(
            paths,
            {
                "mode": "resume" if resume else "run",
                "verdict": VERDICT_INSUFFICIENT,
                "entry_rule": ENTRY_RULE,
                "n_eligible": len(eligible),
                "note": "Fewer than 10 ELIGIBLE_CORE_30D coins — no robust claim.",
                "resources_before": resources_before,
                "resources_after": resource_snapshot(),
                "git": _git_meta(repo),
            },
        )
        return {"ok": True, "verdict": VERDICT_INSUFFICIENT, "summary": summary, "output_dir": str(out_dir)}

    try:
        todo, skipped = symbols_to_process(eligible, paths["checkpoints"], resume=resume)
    except IncompatibleCheckpointError as exc:
        write_manifest(
            paths,
            {
                "mode": "resume" if resume else "run",
                "verdict": VERDICT_FAILED,
                "entry_rule": ENTRY_RULE,
                "error": str(exc),
                "incompatible_checkpoints": True,
            },
        )
        return {
            "ok": False,
            "verdict": VERDICT_FAILED,
            "error": str(exc),
            "output_dir": str(out_dir),
        }

    max_workers = max(1, int(cfg.get("max_workers") or 1))
    results = []

    if max_workers == 1:
        for i, sym in enumerate(todo, 1):
            results.append(
                _process_symbol(
                    sym,
                    start=start,
                    end=end,
                    coverage_class=class_map.get(sym),
                    paths=paths,
                    repo=repo,
                )
            )
            if cfg.get("checkpoint_every") and i % int(cfg["checkpoint_every"]) == 0:
                write_manifest(
                    paths,
                    {
                        "mode": "resume" if resume else "run",
                        "entry_rule": ENTRY_RULE,
                        "progress": {"completed_this_session": i, "todo": len(todo), "skipped": skipped},
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                    },
                )
    else:
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            futs = {
                ex.submit(
                    _process_symbol,
                    sym,
                    start=start,
                    end=end,
                    coverage_class=class_map.get(sym),
                    paths=paths,
                    repo=repo,
                ): sym
                for sym in todo
            }
            for fut in as_completed(futs):
                results.append(fut.result())

    report_payload = collect_and_report(paths, start=start, end=end, n_eligible=len(eligible))
    finished = datetime.now(timezone.utc)
    write_manifest(
        paths,
        {
            "mode": "resume" if resume else "run",
            "cli_argv": cfg.get("cli_argv"),
            "started_at": started.isoformat(),
            "finished_at": finished.isoformat(),
            "git": _git_meta(repo),
            "window": {"start": start.isoformat(), "end": end.isoformat()},
            "strategies": {"primary": PRIMARY_CELLS, "secondary": SECONDARY_STRATEGIES},
            "cost_pct": PRIMARY_COST_PCT,
            "notional_usdt": NOTIONAL_USDT,
            "same_bar_rule": SAME_BAR_RULE,
            "entry_rule": ENTRY_RULE,
            "funding_status": FUNDING_STATUS,
            "eligible": eligible,
            "skipped_complete": skipped,
            "session_results": results,
            "complete_symbols": list_complete_symbols(paths["checkpoints"]),
            "verdict": report_payload.get("verdict"),
            "resources_before": resources_before,
            "resources_after": resource_snapshot(),
            "xrp_frozen_defs": frozen_cells_match_xrp_matrix_defs(),
        },
    )
    (paths["root"] / "resource_report.json").write_text(
        json.dumps({"before": resources_before, "after": resource_snapshot()}, indent=2) + "\n"
    )
    return {"ok": True, "output_dir": str(out_dir), **report_payload}


def collect_and_report(
    paths: dict[str, Path],
    *,
    start: datetime,
    end: datetime,
    n_eligible: int | None = None,
) -> dict[str, Any]:
    trades: list[dict[str, Any]] = []
    coin_stats: list[dict[str, Any]] = []
    complete = list_complete_symbols(paths["checkpoints"])
    for sym in complete:
        rec = load_checkpoint(paths["checkpoints"], sym)
        if not rec:
            continue
        trades.extend(rec.get("trades") or [])
        coin_stats.extend(rec.get("stats_by_strategy") or [])

    if n_eligible is None:
        try:
            n_eligible = len(_load_preflight_eligible(paths))
        except FileNotFoundError:
            n_eligible = len(complete)

    insufficient = n_eligible < MIN_ELIGIBLE_FOR_ROBUST
    summary = build_reports(
        reports_dir=paths["reports"],
        trades=trades,
        coin_stats=coin_stats,
        n_eligible=n_eligible,
        start=start,
        end=end,
        insufficient_coverage=insufficient,
    )

    # Also dump combined trade/candidate tables for audit
    if trades:
        keys = sorted({k for t in trades for k in t.keys()})
        with (paths["reports"] / "trades_all_coins.csv").open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
            w.writeheader()
            for t in trades:
                w.writerow({k: t.get(k) for k in keys})

    cands: list[dict[str, Any]] = []
    for sym in complete:
        rec = load_checkpoint(paths["checkpoints"], sym) or {}
        cands.extend(rec.get("candidates") or [])
    if cands:
        # flatten list fields
        flat = []
        for c in cands:
            row = dict(c)
            for k, v in list(row.items()):
                if isinstance(v, (list, dict)):
                    row[k] = json.dumps(v, default=str)
            flat.append(row)
        keys = sorted({k for c in flat for k in c.keys()})
        with (paths["reports"] / "candidates_all_coins.csv").open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
            w.writeheader()
            for c in flat:
                w.writerow({k: c.get(k) for k in keys})

    return {"verdict": summary.get("verdict"), "summary": summary, "n_trades": len(trades), "n_complete": len(complete)}


def run_report_only(cfg: dict[str, Any]) -> dict[str, Any]:
    repo = _repo_root()
    out_dir = _resolve(cfg["output_dir"], repo)
    paths = ensure_dirs(out_dir)
    start, end = _utc(cfg["start"]), _utc(cfg["end"])
    payload = collect_and_report(paths, start=start, end=end)
    write_manifest(
        paths,
        {
            "mode": "report-only",
            "cli_argv": cfg.get("cli_argv"),
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "git": _git_meta(repo),
            "verdict": payload.get("verdict"),
            "funding_status": FUNDING_STATUS,
            "entry_rule": ENTRY_RULE,
            "same_bar_rule": SAME_BAR_RULE,
        },
    )
    return {"ok": True, "output_dir": str(out_dir), **payload}


def default_cfg(**overrides: Any) -> dict[str, Any]:
    base = {
        "symbols_file": DEFAULT_SYMBOLS_FILE,
        "start": DEFAULT_START,
        "end": DEFAULT_END,
        "output_dir": DEFAULT_OUTPUT_DIR,
        "max_workers": 1,
        "checkpoint_every": 1,
        "limit_symbols": None,
    }
    base.update(overrides)
    return base
